# Environment B - text and container work. No GPU, no ML.
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
RUN pip install --no-cache-dir ebooklib beautifulsoup4 lxml pypdf pysbd ftfy \
      mutagen typer rich

COPY src/ /app/src/
ENV PYTHONPATH=/app/src
ENTRYPOINT ["python3", "-m"]
