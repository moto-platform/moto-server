# moto-server: ingestion, storage and validation of moto-platform ride sessions.
#
# Build context must be the moto-server repo root with the
# external/moto-vehicle-defs submodule already checked out
# (`git submodule update --init --recursive`) -- the defs code is never
# vendored into this repo, so it must be present at build time.

FROM python:3.11-slim AS base

# git is needed at runtime by moto_server.defs.defs_version() (`git describe`
# on the submodule); harmless if that call falls back to defs_pin.txt instead.
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY . .
RUN uv sync --frozen --no-dev

RUN mkdir -p /data
ENV MOTO_DATA_DIR=/data
VOLUME ["/data"]

EXPOSE 8000

CMD ["moto-server", "serve", "--host", "0.0.0.0", "--port", "8000"]
