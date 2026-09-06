# Environment C - Coqui XTTS-v2. Legacy stack frozen by uv.lock.
FROM nvidia/cuda:12.4.1-runtime-ubuntu22.04

RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg libsndfile1 espeak-ng ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-install-project
COPY src/ ./src/
RUN uv sync --frozen

ENV PATH="/app/.venv/bin:$PATH"
ENV COQUI_TOS_AGREED=1
ENTRYPOINT ["python", "-m", "narrator.synth"]
