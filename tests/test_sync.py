"""Tests for the kobo2readwise proxy.

The most important assertion here is the trust guarantee: the Readwise token
must never leak into a response body or into logs.
"""

import logging

import httpx
import respx
from fastapi.testclient import TestClient

from app import READWISE_AUTH_URL, READWISE_URL, app

client = TestClient(app)


def test_healthz_ok():
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "version" in body


def test_index_served():
    resp = client.get("/")
    assert resp.status_code == 200
    assert "kobo2readwise" in resp.text


def test_about_page_served():
    resp = client.get("/about.html")
    assert resp.status_code == 200
    assert "About" in resp.text


def test_html_revalidates():
    # HTML must not be served stale from cache after a deploy.
    assert client.get("/").headers.get("cache-control") == "no-cache"
    assert client.get("/about.html").headers.get("cache-control") == "no-cache"


def test_sqljs_is_self_hosted():
    # sql.js must be served from this app, not a CDN.
    assert client.get("/sqljs/sql-wasm.js").status_code == 200
    wasm = client.get("/sqljs/sql-wasm.wasm")
    assert wasm.status_code == 200
    # Correct MIME type matters for WebAssembly streaming instantiation.
    assert wasm.headers["content-type"] == "application/wasm"


def test_sync_without_token_is_400():
    resp = client.post("/sync", json={"highlights": [{"text": "hello"}]})
    assert resp.status_code == 400


def test_sync_without_highlights_is_400():
    resp = client.post("/sync", json={"token": "abc", "highlights": []})
    assert resp.status_code == 400


def test_verify_without_token_is_400():
    resp = client.post("/verify", json={})
    assert resp.status_code == 400


@respx.mock
def test_verify_valid_token():
    route = respx.get(READWISE_AUTH_URL).mock(return_value=httpx.Response(204))
    resp = client.post("/verify", json={"token": "good-token"})
    assert resp.status_code == 200
    assert resp.json() == {"valid": True}
    assert route.calls.last.request.headers["authorization"] == "Token good-token"


@respx.mock
def test_verify_invalid_token():
    respx.get(READWISE_AUTH_URL).mock(return_value=httpx.Response(401))
    resp = client.post("/verify", json={"token": "bad-token"})
    assert resp.status_code == 200
    assert resp.json() == {"valid": False}


@respx.mock
def test_verify_never_leaks_token(caplog):
    respx.get(READWISE_AUTH_URL).mock(return_value=httpx.Response(204))
    token = "verify-secret-should-not-leak-42"
    with caplog.at_level(logging.DEBUG):
        resp = client.post("/verify", json={"token": token})
    assert token not in resp.text
    assert token not in caplog.text


@respx.mock
def test_sync_success_forwards_and_counts():
    route = respx.post(READWISE_URL).mock(return_value=httpx.Response(200, json={}))
    resp = client.post(
        "/sync",
        json={"token": "secret-token", "highlights": [{"text": "a"}, {"text": "b"}]},
    )
    assert resp.status_code == 200
    assert resp.json() == {"synced": 2}

    # Token is forwarded to Readwise in the Authorization header, nowhere else.
    assert route.called
    sent = route.calls.last.request
    assert sent.headers["authorization"] == "Token secret-token"


@respx.mock
def test_sync_upstream_401_maps_to_401():
    respx.post(READWISE_URL).mock(return_value=httpx.Response(401, json={"detail": "nope"}))
    resp = client.post("/sync", json={"token": "bad", "highlights": [{"text": "a"}]})
    assert resp.status_code == 401


@respx.mock
def test_token_never_leaks_to_response_or_logs(caplog):
    respx.post(READWISE_URL).mock(return_value=httpx.Response(200, json={}))
    token = "super-secret-do-not-leak-1234567890"
    with caplog.at_level(logging.DEBUG):
        resp = client.post("/sync", json={"token": token, "highlights": [{"text": "a"}]})

    assert resp.status_code == 200
    assert token not in resp.text
    assert token not in caplog.text
