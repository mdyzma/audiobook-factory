# Environment B - text and container work. No GPU, no ML.
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-install-project
COPY src/ ./src/
RUN uv sync --frozen

ENV PATH="/app/.venv/bin:$PATH"
ENTRYPOINT ["python", "-m"]
