# audiobook-factory

[![CI](https://github.com/mdyzma/audiobook-factory/actions/workflows/ci.yml/badge.svg)](https://github.com/mdyzma/audiobook-factory/actions/workflows/ci.yml)

Clone a voice from a recording, then read an ebook aloud in it.

Five stages, three isolated Python environments, one `just` command runner.

```
sample.mp3 ──▶ clean ──▶ label ──▶ clone ─────────┐
                                                   │  voice profile + latents
book.epub ──▶ ingest ──▶ chunk ──▶ synth ──▶ assemble ──▶ book.m4b
                          │          │
                    chunks.jsonl   wav per chunk
```

## Why three environments

`whisperx` needs pandas 2.x. `tts` 0.22.0 needs pandas 1.x. They cannot share a
virtualenv, so they do not. The `data/` directory is the only thing they share,
and the chunk manifest is the contract between them. See [docs/DECISIONS.md](docs/DECISIONS.md).

| Directory | Role | Stack |
|---|---|---|
| `transcriber/` | Cuts and labels the voice sample | WhisperX, numpy 2.x |
| `bookbinder/` | Ebook parsing, chunking, final mux | pure Python, ffmpeg |
| `narrator/` | Voice cloning and speech synthesis | Coqui XTTS-v2, numpy 1.x |

Each is a uv project with its own `uv.lock`, so both machines install byte-identical
dependency sets.

## Requirements

`uv`, `just` and `ffmpeg`. uv installs Python 3.11.9 itself, so there is no
pyenv or poetry to set up. A CUDA GPU makes synthesis roughly an order of
magnitude faster but is not required.

## Setup

```bash
just setup
just doctor
```

Day-to-day workflow, dependency rules and failure modes:
[docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).

## Tests and types

```bash
just check          # pyright + pytest across all three environments
just test           # tests only
just typecheck      # types only
just test-one narrator -k formatter
```

Each environment type-checks against its own installed dependencies, which is
the point of the split: the narrator's numpy 1.x and the transcriber's numpy 2.x
are checked separately, as they are installed.

## Producing an audiobook

Drop a voice recording in `data/raw/voices/` and an ebook in `data/raw/books/`,
then run the whole thing:

```bash
just factory data/raw/voices/michal.mp3 michal data/raw/books/lem.epub solaris pl
```

Or one stage at a time:

```bash
just clean data/raw/voices/michal.mp3 michal   # denoise, 24 kHz mono
just label michal                              # WhisperX cuts and transcribes
just clone michal                              # speaker latents + audition clip
open data/voices/michal/audition.wav           # judge the clone before committing

just train michal                              # optional full fine-tune, CUDA only

just ingest data/raw/books/lem.epub solaris    # epub/pdf/txt -> chapters.json
just chunk solaris michal                      # -> chunks.jsonl with metadata
just preview solaris michal                    # render 20 chunks as a smoke test
just synth solaris michal                      # render everything, resumable
just assemble solaris                          # -> data/out/solaris.m4b
```

`just synth` skips chunks that already have audio, so re-running it after a crash
continues rather than starting over.

## Layout

```
config/pipeline.toml      every tunable knob, read by all stages
data/raw/                 your inputs
data/processed/           cleaned 24 kHz voice audio
data/datasets/<voice>/    wavs + metadata.csv for cloning or fine-tuning
data/voices/<voice>.json  voice profile; latents cached alongside
data/book/<slug>/         chapters.json, chunks.jsonl, book.json
data/audio/<slug>/        one wav per chunk, plus rendered.jsonl
data/out/                 the finished audiobook
```

## Docker

For the CUDA host only. Apple Silicon gets no GPU passthrough, so use the native
Poetry environments there.

```bash
just build-images
just up transcriber
```
