# Environment C - Coqui XTTS-v2. Legacy stack frozen inside the image.
FROM nvidia/cuda:12.4.1-runtime-ubuntu22.04

RUN apt-get update && apt-get install -y --no-install-recommends \
      python3.11 python3-pip ffmpeg libsndfile1 espeak-ng \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
RUN pip install --no-cache-dir torch torchaudio --index-url https://download.pytorch.org/whl/cu124
RUN pip install --no-cache-dir "numpy<2.0.0" "pandas<2.0.0" "pydantic<2.10.0" \
      soundfile librosa tqdm typer
RUN pip install --no-cache-dir "TTS==0.22.0" --no-deps \
    && pip install --no-cache-dir anyascii coqpit cython encodec inflect num2words \
       pysbd trainer umap-learn unidecode jieba pypinyin \
    && pip install --no-cache-dir "gruut[all]==2.2.3" --no-deps \
    && pip install --no-cache-dir "numpy<2.0.0" --force-reinstall

COPY src/ /app/src/
ENV PYTHONPATH=/app/src
ENV COQUI_TOS_AGREED=1
ENTRYPOINT ["python3", "-m", "narrator.synth"]
