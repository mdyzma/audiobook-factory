# Environment A - WhisperX. CUDA base so the RTX 5090 is usable.
FROM nvidia/cuda:12.4.1-runtime-ubuntu22.04

RUN apt-get update && apt-get install -y --no-install-recommends \
      python3.11 python3-pip ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
RUN pip install --no-cache-dir torch torchaudio --index-url https://download.pytorch.org/whl/cu124
RUN pip install --no-cache-dir whisperx==3.8.1 pandas numpy pydub tqdm typer

COPY src/ /app/src/
ENV PYTHONPATH=/app/src
ENTRYPOINT ["python3", "-m", "transcriber.auto_label"]
