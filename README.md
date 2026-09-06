# audiobook-factory

[![CI](https://github.com/mdyzma/audiobook-factory/actions/workflows/ci.yml/badge.svg)](https://github.com/mdyzma/audiobook-factory/actions/workflows/ci.yml)

Clone a voice from a recording, then read an ebook aloud in it. Dialogue can
be cast to different voices.

```
sample.mp3 ──▶ clean ──▶ label ──▶ clone ─────────┐
                                                   │  voice profile + latents
book.epub ──▶ ingest ──▶ chunk ──▶ synth ──▶ assemble ──▶ book.m4b
                          │          │                        │
                    chunks.jsonl   wav per chunk           verify
                    + cast roles   + report.json          (optional)
```

## Quickstart

You need `uv`, `just` and `ffmpeg`. uv installs Python itself, so there is
nothing else to set up.

```bash
brew install uv just ffmpeg
git clone git@github.com:mdyzma/audiobook-factory.git
cd audiobook-factory
just setup
```

Put a voice recording in `data/raw/voices/` and an ebook in `data/raw/books/`,
then run the whole pipeline:

```bash
just factory data/raw/voices/michal.mp3 michal data/raw/books/lem.epub solaris pl
```

The finished audiobook lands in `data/out/solaris.m4b` with chapter marks.

Check the structure first, in seconds and with no model loaded:

```bash
just book-dry data/raw/books/lem.epub solaris
```

That renders silence at each fragment's estimated duration and assembles it, so
chapter marks, pauses and the cast are all verifiable before anything slow runs.

Before committing hours to a voice, hear it first:

```bash
just voice data/raw/voices/michal.mp3 michal   # clean, label, clone
open data/voices/michal/audition.wav           # judge the clone
just book data/raw/books/lem.epub michal solaris
```

`just synth` skips fragments that already have audio, so interrupting it is
safe and re-running continues where it stopped.

The commands you need day to day:

| Command | What it does |
|---|---|
| `just` | List every recipe |
| `just setup` | Install everything, once per machine |
| `just doctor` | Show what each environment resolved |
| `just check` | Schemas, types and tests, about 8 seconds |
| `just preview <slug> <voice>` | Render 20 fragments to sample the voice |
| `just book-dry <source> <slug>` | Whole structure with silence, no models |

Full reference: [docs/COMMANDS.md](docs/COMMANDS.md).

## Casting voices

Every paragraph gets a role. Narration goes to `narrator`, quoted or dashed
speech to `dialogue`, and a line labelled with a speaker ("Kelvin: — Wracam na
Ziemię") to that character. `config/cast.yml` maps roles to cloned voices:

```yaml
roles:
  narrator:
    voice: michal
  dialogue:
    voice: michal
    speed: 1.02
  kelvin:
    voice: kelvin
```

Roles with no entry fall back to the narrator, so a single-voice audiobook needs
nothing but the narrator line, and `just chunk-single` skips casting entirely.

Detection is typographic, not semantic, and deliberately cautious: narration
misread in a character's voice is far more jarring than dialogue left with the
narrator, so anything ambiguous stays with the narrator.

## Checking the result

Synthesis fails quietly. XTTS can truncate a fragment, skip a clause or repeat
a phrase without raising anything, and nobody listens to twenty hours before
publishing.

```bash
just verify solaris 20     # re-transcribe every 20th fragment and compare
```

Every render also writes `data/audio/<slug>/report.json` with what was rendered,
what was skipped, what failed, and the realtime factor.

## Tests and types

```bash
just check          # schemas + pyright + pytest across all three environments
just test           # tests only, about 3 seconds
just typecheck      # types only
just test-one narrator -k formatter
```

| Environment | Tests |
|---|---|
| bookbinder | 104 |
| transcriber | 20 |
| narrator | 17 |

Each environment type-checks against its own installed dependencies, which is
the point of the split: the narrator's numpy 1.x and the transcriber's numpy 2.x
are checked separately, as they are installed.

Nothing in the suite needs model weights or a GPU, so it finishes in seconds.
The pipeline is still covered end to end, because the dry-run renderer stands in
for synthesis. What the suite cannot reach is covered by `just check-narrator`,
which loads XTTS for real, and by fine-tuning, which needs CUDA.

## Why three environments

`whisperx` requires pandas 2.x. `tts` 0.22.0 requires pandas 1.x. There is no
resolution, and forcing one produces an environment where XTTS fails at
inference because numpy 2.x changed its API.

So they never share a virtualenv. Each is a separate uv project with its own
lock, and `data/` is the only thing they have in common. The full reasoning,
including every pin and why it is load-bearing, is in
[docs/DECISIONS.md](docs/DECISIONS.md).

| Environment | Role | numpy | pandas |
|---|---|---|---|
| `transcriber/` | Cuts and labels the voice sample | 2.4.6 | 3.0.5 |
| `bookbinder/` | Ebook parsing, chunking, final mux | none | none |
| `narrator/` | Voice cloning and speech synthesis | 1.26.4 | 1.5.3 |

## Repository layout

```
audiobook-factory/
├── transcriber/          Environment A - WhisperX, numpy 2.x
│   ├── pyproject.toml      manifest + pyright/pytest config
│   ├── uv.lock             committed; what CI and the GPU box install
│   ├── Dockerfile          CUDA base image
│   ├── src/transcriber/
│   │   ├── auto_label.py   cuts and transcribes the voice sample
│   │   └── verify.py       re-transcribes the render, reports word error rate
│   └── tests/
│
├── bookbinder/           Environment B - text and containers, no ML
│   └── src/bookbinder/
│       ├── ingest.py       EPUB/PDF/text -> normalised chapters
│       ├── roles.py        who speaks each paragraph
│       ├── cast.py         role -> voice, with a narrator fallback
│       ├── chunk.py        chapters -> fragments under the XTTS limit
│       ├── manifest.py     the validated contract between environments
│       ├── schemas.py      exports that contract to docs/schemas/
│       ├── dryrun.py       silence at the right durations, for structure checks
│       └── assemble.py     fragments + pauses -> chaptered m4b
│
├── narrator/             Environment C - Coqui XTTS-v2, numpy 1.x
│   └── src/narrator/
│       ├── engine.py       model loading, speaker latents
│       ├── clone.py        speaker embedding + audition clip
│       ├── synth.py        fragments -> audio; multi-voice, resumable
│       └── train.py        optional fine-tune, CUDA only
│
├── config/
│   ├── pipeline.toml     Every tunable knob. Read by all stages.
│   └── cast.yml          Which voice reads which role.
├── justfile              Every command. Nothing is run directly.
├── scripts/              ffmpeg preprocessing
├── docs/
│   ├── COMMANDS.md       every recipe
│   ├── DEVELOPMENT.md    workflow and failure modes
│   ├── DECISIONS.md      why the pins and the split exist
│   └── schemas/          JSON Schema, generated from manifest.py
├── docker-compose.yml    CUDA host only; no GPU passthrough on macOS
│
├── data/                 Everything below here is gitignored
│   ├── raw/              your inputs: voices/ and books/
│   ├── processed/        cleaned 24 kHz voice audio
│   ├── datasets/         wavs + metadata.csv for cloning or fine-tuning
│   ├── voices/           voice profiles and cached latents
│   ├── book/             chapters.json, chunks.jsonl, book.json
│   ├── audio/            one wav per fragment, rendered.jsonl, report.json
│   └── out/              the finished audiobook
│
└── training/             Fine-tune checkpoints and logs. Gitignored.
```

The three `src/` trees never import each other. They communicate only through
files under `data/`. The shape of those files is defined in
`bookbinder/src/bookbinder/manifest.py` and exported to `docs/schemas/`, which
is what keeps the two environments that cannot import it in step.

## Documentation

| Document | What is in it |
|---|---|
| [docs/COMMANDS.md](docs/COMMANDS.md) | Every `just` recipe and its arguments |
| [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) | Day-to-day workflow, adding dependencies safely, failure modes |
| [docs/DECISIONS.md](docs/DECISIONS.md) | Why the environments are split, and every pin that keeps XTTS working |
| [docs/schemas/](docs/schemas/) | JSON Schema for every file that crosses an environment boundary |

## Hardware

Everything runs on Apple Silicon via MPS, but synthesis measures about 0.8x
realtime there, so a ten-hour book takes roughly twelve hours. That workload
belongs on a CUDA machine, where `just gpu-torch narrator` swaps in the CUDA
wheels.

Fine-tuning requires CUDA outright. Instant cloning does not, and is good
enough for most books.

## Licence

MIT. See [LICENSE](LICENSE).
