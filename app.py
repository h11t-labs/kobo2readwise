"""kobo2readwise — a thin, stateless proxy that forwards Kobo highlights to Readwise.

Trust model (the whole point of this app):

* The Readwise API token is forwarded to Readwise and then forgotten.
* It is NEVER logged, stored, cached, or written to disk.
* Request bodies are not logged anywhere in this process.

If you touch this file, keep it that way. No ``print(payload)``, no request-body
logging middleware, no persistence. The token lives only in memory for the
duration of a single request.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

READWISE_URL = "https://readwise.io/api/v2/highlights/"
READWISE_AUTH_URL = "https://readwise.io/api/v2/auth/"
BATCH_SIZE = 100
# Configurable so tests / self-hosters can tune it; defaults to gentle per-IP
# limits that protect against abuse and runaway egress costs.
SYNC_RATE_LIMIT = os.environ.get("SYNC_RATE_LIMIT", "10/hour")
VERIFY_RATE_LIMIT = os.environ.get("VERIFY_RATE_LIMIT", "30/hour")
# Bound egress and memory: cap highlights per request and total request size.
MAX_HIGHLIGHTS = int(os.environ.get("MAX_HIGHLIGHTS", "5000"))
MAX_BODY_BYTES = int(os.environ.get("MAX_BODY_BYTES", str(16 * 1024 * 1024)))
STATIC_DIR = Path(__file__).parent / "static"

# A Readwise token is a short, printable-ASCII string. Reject anything with
# whitespace or control characters *before* it becomes an HTTP header value:
# an illegal header makes httpx raise with the token in the message, which would
# then be logged — breaking the token-never-logged guarantee.
_TOKEN_RE = re.compile(r"[\x21-\x7e]{1,256}")


def _valid_token(token: str) -> bool:
    return _TOKEN_RE.fullmatch(token) is not None


def _version() -> str:
    """Best-effort app version so release-please bumps are visible at runtime."""
    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            return version("kobo2readwise")
        except PackageNotFoundError:
            pass
    except Exception:
        pass
    # Fallback for non-installed runs (local dev, container): read pyproject.toml.
    try:
        import tomllib

        with open(Path(__file__).parent / "pyproject.toml", "rb") as fh:
            return tomllib.load(fh)["project"]["version"]
    except Exception:
        return "0.0.0"


__version__ = _version()


def _client_ip(request: Request) -> str:
    """Real client IP for rate limiting.

    Behind Fly's proxy the socket peer is the proxy, so prefer the
    ``Fly-Client-IP`` header, then ``X-Forwarded-For``, then the socket peer.
    """
    forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    return request.headers.get("fly-client-ip") or forwarded or get_remote_address(request)


limiter = Limiter(key_func=_client_ip)

# Docs UIs are disabled on purpose: /docs and /redoc load Swagger/ReDoc JS from a
# CDN, which would run third-party script on our origin and could read a
# remembered token from localStorage. This is a plain proxy — it needs no docs.
app = FastAPI(
    title="kobo2readwise",
    version=__version__,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.state.limiter = limiter


@app.exception_handler(RateLimitExceeded)
async def _rate_limit_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content={"detail": "Rate limit exceeded. Please try again later."},
    )


@app.middleware("http")
async def _revalidate_html(request: Request, call_next):
    """Make browsers revalidate HTML so a deploy never serves a stale page.

    Static assets are otherwise heuristically cached by the browser, which can
    pin an old index.html after an update. ``no-cache`` still allows efficient
    304s — it just forbids using the cache without checking first.
    """
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.endswith(".html"):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.middleware("http")
async def _limit_body_size(request: Request, call_next):
    """Reject oversized bodies before FastAPI parses them.

    The rate limiter runs inside the endpoint (after parsing), so it can't guard
    the parser. A Content-Length check here caps memory/egress for the common
    (non-chunked) case.
    """
    if request.method == "POST":
        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > MAX_BODY_BYTES:
            return JSONResponse(status_code=413, content={"detail": "Request body too large."})
    return await call_next(request)


class SyncRequest(BaseModel):
    token: str = Field(default="")
    highlights: list[dict] = Field(default_factory=list)


class VerifyRequest(BaseModel):
    token: str = Field(default="")


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok", "version": __version__}


@app.post("/verify")
@limiter.limit(VERIFY_RATE_LIMIT)
async def verify(request: Request, payload: VerifyRequest) -> dict:
    """Check a Readwise token against Readwise's auth endpoint (204 == valid).

    Lets the UI show a "Connected" state before syncing. Like /sync, the token
    is forwarded to Readwise and then forgotten — never logged or stored.
    """
    token = payload.token.strip()
    if not token:
        raise HTTPException(status_code=400, detail="Missing Readwise token.")
    if not _valid_token(token):
        raise HTTPException(
            status_code=400, detail="That doesn't look like a valid Readwise token."
        )
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(READWISE_AUTH_URL, headers={"Authorization": f"Token {token}"})
    except httpx.HTTPError:
        # Never surface the exception text — it can echo the token/header value.
        raise HTTPException(
            status_code=502, detail="Couldn't reach Readwise. Please try again."
        ) from None
    if resp.status_code == 204:
        return {"valid": True}
    if resp.status_code == 401:
        return {"valid": False}
    raise HTTPException(
        status_code=502, detail=f"Couldn't reach Readwise (HTTP {resp.status_code})."
    )


@app.post("/sync")
@limiter.limit(SYNC_RATE_LIMIT)
async def sync(request: Request, payload: SyncRequest) -> dict:
    token = payload.token.strip()
    if not token:
        raise HTTPException(status_code=400, detail="Missing Readwise token.")
    if not _valid_token(token):
        raise HTTPException(
            status_code=400, detail="That doesn't look like a valid Readwise token."
        )
    if not payload.highlights:
        raise HTTPException(status_code=400, detail="No highlights to sync.")
    if len(payload.highlights) > MAX_HIGHLIGHTS:
        raise HTTPException(
            status_code=413,
            detail=f"Too many highlights in one request (max {MAX_HIGHLIGHTS}).",
        )

    headers = {"Authorization": f"Token {token}"}
    synced = 0
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            for start in range(0, len(payload.highlights), BATCH_SIZE):
                batch = payload.highlights[start : start + BATCH_SIZE]
                resp = await client.post(READWISE_URL, headers=headers, json={"highlights": batch})
                if resp.status_code == 401:
                    raise HTTPException(status_code=401, detail="Readwise rejected the token.")
                if not 200 <= resp.status_code < 300:
                    raise HTTPException(
                        status_code=502,
                        detail=f"Readwise returned an error (HTTP {resp.status_code}).",
                    )
                synced += len(batch)
    except httpx.HTTPError:
        # Never surface the exception text — it can echo the token/header value.
        raise HTTPException(
            status_code=502, detail="Couldn't reach Readwise. Please try again."
        ) from None
    return {"synced": synced}


# Serve the single-page frontend as same-origin static files (no CORS needed).
# Mounted last so the API routes above take precedence.
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
