# Environment D - the dashboard. No GPU, no ML.
#
# It depends on bookbinder by path, so both projects are copied and the build
# context is the repository root.
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.9.18 /uv /usr/local/bin/uv

ENV AUDIOBOOK_FACTORY_ROOT=/app \
    UV_LINK_MODE=copy \
    UV_PYTHON_INSTALL_DIR=/opt/python \
    UV_COMPILE_BYTECODE=1

WORKDIR /app/apps/studio

# bookbinder first: it is a path dependency, so uv needs it present before it
# can resolve studio at all.
COPY apps/bookbinder/pyproject.toml apps/bookbinder/uv.lock apps/bookbinder/.python-version /app/apps/bookbinder/
COPY apps/bookbinder/src /app/apps/bookbinder/src

COPY apps/studio/pyproject.toml apps/studio/uv.lock apps/studio/.python-version ./
RUN uv sync --frozen --no-install-project --no-dev

COPY apps/studio/src ./src
RUN uv sync --frozen --no-dev

# The dashboard can start pipeline stages, which it does by running `just`.
# That binary is not in this image, so inside a container it is a read-only
# view: browsing, listening and reports work, buttons do not.
EXPOSE 8765
ENTRYPOINT ["uv", "run", "--frozen", "--no-dev"]
CMD ["python", "-m", "studio", "--host", "0.0.0.0", "--port", "8765"]
