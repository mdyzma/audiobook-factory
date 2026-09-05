#!/usr/bin/env bash
# Stage 1a - clean a raw voice recording into XTTS-ready audio.
# Usage: scripts/preprocess.sh data/raw/voices/michal.mp3 michal
set -euo pipefail

INPUT="${1:?usage: preprocess.sh <input-audio> <voice-name>}"
NAME="${2:?usage: preprocess.sh <input-audio> <voice-name>}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="$ROOT/data/processed/$NAME"
mkdir -p "$OUT_DIR"

# afftdn strips microphone hiss; loudnorm evens out level. Training on noisy
# audio teaches the clone to reproduce the noise, which is the usual amateur mistake.
ffmpeg -hide_banner -y -i "$INPUT" \
  -af "afftdn=nf=-25,loudnorm=I=-19:TP=-2:LRA=9" \
  -ar 24000 -ac 1 -c:a pcm_s16le \
  "$OUT_DIR/cleaned_full.wav"

DURATION=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$OUT_DIR/cleaned_full.wav")
printf 'cleaned -> %s (%.1f min, 24kHz mono s16)\n' "$OUT_DIR/cleaned_full.wav" "$(echo "$DURATION/60" | bc -l)"
