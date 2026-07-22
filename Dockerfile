# Build on Astral's uv image (Python 3.14, Debian slim).
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim

# uv tuning: use the copy link mode and don't try to manage Python itself.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Install dependencies first (cached layer) using only the lock + manifest.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# Copy the application.
COPY app.py ./
COPY static ./static

EXPOSE 8000

# Fly maps the public HTTPS port to this internal port (see fly.toml).
CMD ["uv", "run", "--no-dev", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
