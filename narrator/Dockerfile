# Environment C - Coqui XTTS-v2. numpy 1.x, frozen by uv.lock.
#
# CUDA 12.8, not 12.4: Blackwell cards such as the RTX 5090 are compute
# capability sm_120, and 12.4 builds contain no kernels for them. See
# docs/HANDOFF-GPU.md.
#
# UNVERIFIED. This image has never been built: the machine it was written on is
# Apple Silicon, and these bases are amd64 only. Treat the first build as work.
FROM nvidia/cuda:12.8.1-runtime-ubuntu22.04

RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg libsndfile1 espeak-ng ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.9.18 /uv /usr/local/bin/uv

ENV UV_LINK_MODE=copy \
    UV_PYTHON_INSTALL_DIR=/opt/python \
    UV_COMPILE_BYTECODE=1 \
    COQUI_TOS_AGREED=1

WORKDIR /app/narrator

COPY narrator/pyproject.toml narrator/uv.lock narrator/.python-version ./
RUN uv sync --frozen --no-install-project --no-dev

COPY narrator/src ./src
RUN uv sync --frozen --no-dev

# `uv sync` installs the torch the lock names, from the default index. On a
# Blackwell card that build may lack sm_120 kernels, in which case swap it here
# and re-lock rather than drifting the pinned version:
#   uv pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu128
# Verify with torch.cuda.get_device_capability() == (12, 0), not is_available().

# Model weights are ~1.7 GB and download on first use; mount a volume at
# /root/.local/share/tts so a rebuild does not fetch them again.
ENTRYPOINT ["uv", "run", "--frozen", "--no-dev"]
CMD ["python", "-c", "import TTS; print('narrator image ok')"]
