# Environment A - WhisperX. CUDA base so the RTX 5090 is usable.
FROM nvidia/cuda:12.4.1-runtime-ubuntu22.04

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
# Lockfile first: dependencies re-resolve only when the manifest changes.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-install-project
COPY src/ ./src/
RUN uv sync --frozen

ENV PATH="/app/.venv/bin:$PATH"
ENTRYPOINT ["python", "-m", "transcriber.auto_label"]
