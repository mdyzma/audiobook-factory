# Environment A - WhisperX. numpy 2.x, frozen by uv.lock.
#
# CUDA 12.8 for the same Blackwell reason as the narrator.
#
# UNVERIFIED. Never built; these bases are amd64 only and this was written on
# Apple Silicon.
FROM nvidia/cuda:12.8.1-runtime-ubuntu22.04

RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.9.18 /uv /usr/local/bin/uv

ENV AUDIOBOOK_FACTORY_ROOT=/app \
    UV_LINK_MODE=copy \
    UV_PYTHON_INSTALL_DIR=/opt/python \
    UV_COMPILE_BYTECODE=1

WORKDIR /app/apps/transcriber

COPY apps/transcriber/pyproject.toml apps/transcriber/uv.lock apps/transcriber/.python-version ./
RUN uv sync --frozen --no-install-project --no-dev

COPY apps/transcriber/src ./src
RUN uv sync --frozen --no-dev

# Whisper weights download on first use; mount a volume at /root/.cache
# to keep them across rebuilds.
ENTRYPOINT ["uv", "run", "--frozen", "--no-dev"]
CMD ["python", "-c", "import whisperx; print('transcriber image ok')"]
