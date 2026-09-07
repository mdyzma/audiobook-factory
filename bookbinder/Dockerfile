# Environment B - text and containers. No GPU, no ML.
#
# Built from the repository root, because studio depends on this project by
# path and every image should see the same layout.
#
# ffmpeg comes from Debian bookworm, which pins it at 5.1. That is a deliberate
# choice rather than "whatever is newest": FFmpeg major versions have already
# broken this project once, when torchcodec could not find libavutil 61.
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && ffmpeg -version | head -1

COPY --from=ghcr.io/astral-sh/uv:0.9.18 /uv /usr/local/bin/uv

# uv installs its own CPython, the same 3.11.9 the host uses, so the container
# is not merely similar to a working machine but identical to one.
ENV UV_LINK_MODE=copy \
    UV_PYTHON_INSTALL_DIR=/opt/python \
    UV_COMPILE_BYTECODE=1

# The modules find the project root as parents[3] of their own file, so the
# layout inside the image has to mirror the repository: /app/bookbinder/src/...
# leaves /app as the root, where data/ and config/ are mounted.
WORKDIR /app/bookbinder

# Dependencies first, from the committed lock. A code change must not
# re-resolve anything.
COPY bookbinder/pyproject.toml bookbinder/uv.lock bookbinder/.python-version ./
RUN uv sync --frozen --no-install-project --no-dev

COPY bookbinder/src ./src
RUN uv sync --frozen --no-dev

# No hard-coded stage: one image serves ingest, chunk, dryrun and assemble.
#   docker compose run bookbinder python -m bookbinder.chunk solaris
ENTRYPOINT ["uv", "run", "--frozen", "--no-dev"]
CMD ["python", "-c", "import bookbinder; print('bookbinder image ok')"]
