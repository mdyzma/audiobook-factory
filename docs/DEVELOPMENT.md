# Local development

How to work on this repo day to day. For *why* the environments are split and
which pins are load-bearing, read [DECISIONS.md](DECISIONS.md) first.

## Prerequisites

| Tool | Why |
|---|---|
| `uv` | Installs Python, manages the three virtualenvs, owns the locks |
| `just` | Every command in this project is a recipe |
| `ffmpeg` | Audio cleaning, silence generation, final mux |

Nothing else. There is no pyenv and no poetry; uv downloads its own CPython.

```bash
brew install uv just ffmpeg
```

## First run

```bash
just setup     # installs 3.11.9 and all three environments
just doctor    # prints the versions each environment resolved
just check     # pyright + pytest, ~7 s
```

Budget roughly 2.7 GB of virtualenvs plus 1.7 GB of model weights on first
synthesis.

| Path | Size | Notes |
|---|---|---|
| `narrator/.venv` | 1.6 GB | 149 packages, torch dominates |
| `transcriber/.venv` | 1.0 GB | 109 packages |
| `bookbinder/.venv` | 77 MB | 25 packages, no ML |
| `~/.local/share/uv/python/` | 59 MB | the interpreter itself |
| `~/Library/Application Support/tts/` | 1.7 GB | XTTS-v2 weights, downloaded on demand |

The model cache sits outside the repo and is shared by every project on the
machine, so deleting a virtualenv does not force a re-download.

## Layout

```
transcriber/   env A   WhisperX          numpy 2.x, pandas 3.x
bookbinder/    env B   text + ffmpeg     no torch
narrator/      env C   Coqui XTTS-v2     numpy 1.x, pandas 1.x
```

Each is a self-contained uv project: its own `pyproject.toml`, its own
`uv.lock`, its own `.venv`, its own `tests/`. They never import each other.
The only thing they share is files under `data/`.

The contract is `bookbinder/src/bookbinder/manifest.py`. If you change the
chunk schema there, update both consumers: `narrator/synth.py` writes
`audio_path` and `duration_sec` into it, and `bookbinder/assemble.py` reads
them back.

## Everyday commands

Full reference: [COMMANDS.md](COMMANDS.md). The ones that matter while working:

```bash
just                      # list every recipe
just check                # types + tests; run this before committing
just test-one narrator -k formatter     # one env, verbose, filtered
just preview myslug myvoice             # 20 fragments, to hear the voice
```

`just synth` skips fragments that already have a wav, so interrupting it is
safe and re-running continues. Use `just clean-audio <slug>` to force a full
re-render.

## Working without models

The heavy path needs a 1.6 GB environment, model weights and hours of GPU time.
Most defects are structural, so most of the time you do not need any of that:

```bash
just book-dry data/raw/books/x.epub myslug
```

That renders silence at each fragment's estimated duration and assembles it,
catching chapter marks in the wrong place, pauses that do not add up, and roles
mapped to voices that do not exist. It is also what lets CI cover the pipeline
end to end in the environment with no torch in it.

`just dryrun <slug> strict` turns an uncloned voice into a failure rather than a
note, which is what you want once the cast is meant to be complete.

## The contract between environments

`bookbinder/src/bookbinder/manifest.py` holds pydantic models for everything
that crosses an environment boundary. `just schemas` exports them to
`docs/schemas/`, and `just schemas-check` fails if the exported files have
drifted. Both run as part of `just check` and in CI.

narrator and transcriber cannot import those models, since they resolve a
different numpy. They mirror the shape by hand and write plain JSON. That is the
one place a silent divergence can appear, so `bookbinder/tests/test_schemas.py`
validates a copy of exactly what each of them writes. If you change a model,
update the writer and that fixture together.

## Adding a dependency

Always inside the environment that needs it, never at the repo root:

```bash
cd bookbinder && uv add ebooklib
cd narrator   && uv add --dev pytest-cov
```

**The narrator is different.** Its `[tool.uv] constraint-dependencies` block is
what keeps XTTS working. Before adding anything there, check whether the new
package drags numpy above 2.0:

```bash
cd narrator && uv add <package> && uv run python -c "import numpy; print(numpy.__version__)"
```

If that prints anything other than `1.26.4`, the addition is unsafe. Add the
offending transitive package to `constraint-dependencies` with a comment saying
why, or do without. `just check-narrator` is the real proof, since it loads
XTTS-v2 rather than merely importing it.

After changing a pin:

```bash
just relock narrator
```

## Testing

Tests live in `<env>/tests/` and run against that environment's own installed
dependencies. That is deliberate: the narrator is type-checked with numpy 1.x
and the transcriber with numpy 2.x, exactly as they are installed.

Anything requiring model weights or CUDA stays out of the suite, which is why
it finishes in about three seconds. The two things it cannot cover:

- **XTTS loading.** Use `just check-narrator`; it downloads the weights once
  and loads them for real.
- **Fine-tuning.** Needs CUDA. `narrator/train.py` refuses to start otherwise,
  and the first genuine run has to happen on the GPU machine.

When adding tests, prefer real ffmpeg over mocks for anything audio. The
assembly tests generate tones and assert chapter timestamps, because a mark
landing at the wrong second is the failure that actually matters and a mock
would hide it.

## CI

`.github/workflows/ci.yml` runs on pushes to `main` and on pull requests.

| Job | What it does |
|---|---|
| `locks` | `uv lock --check` on all three; fails fast if a lock drifted from its manifest |
| `justfile` | `just --list`, so a syntax error is caught without installing anything |
| `check` | Matrix over the three environments: sync, pyright, pytest |

The matrix gives each environment its own runner. That is not just tidiness:
on Linux the torch wheel pulls its CUDA runtime libraries, so the narrator and
transcriber are roughly 4 GB each, and those jobs delete the image's unused
toolchains before syncing to make room. `bookbinder` needs none of that and
also installs ffmpeg, since its assembly tests shell out to it for real.

`locks` gates the matrix, so a stale lock fails in seconds rather than after
ten minutes of installing torch. uv's cache is keyed per environment on its own
`uv.lock`.

If you change a dependency, commit the regenerated lock in the same commit or
CI will fail on the `locks` job.

## Working across two machines

The stages are independent, so labelling and synthesis can run in different
places. Move `data/` between them; nothing else is needed.

On the CUDA box, after `just setup`:

```bash
just gpu-torch narrator
just gpu-torch transcriber
```

That swaps in the CUDA 12.4 wheels. Skip it on Apple Silicon, where the default
PyPI wheels already carry MPS.

Measured on an M1, synthesis runs at roughly 0.8x realtime, so a ten-hour book
takes about twelve hours. That workload belongs on the GPU.

Docker is for the CUDA host only. Compose passes the GPU through, which does
not work on macOS, where Docker runs inside a Linux VM with no Metal access.

## When something breaks

**`numpy 2.x` in the narrator.** Something you installed lifted it. Check with
`just doctor`; fix by constraining the culprit in `constraint-dependencies`,
then `just relock narrator`.

**`Could not load libtorchcodec`.** torchaudio 2.9+ routes audio through
torchcodec, which needs to find FFmpeg's shared objects at runtime, and uv's
standalone CPython has no Homebrew rpath. torch is pinned to 2.8.0 to avoid
this. If you lift that pin, expect it back.

**`UnpicklingError` loading a checkpoint.** PyTorch 2.6 defaults `torch.load`
to `weights_only=True`. `narrator.engine.allow_xtts_globals()` allowlists the
XTTS config classes; call it before loading any XTTS checkpoint. Do not reach
for `weights_only=False`, which disables the protection process-wide.

**`cannot import name 'BeamSearchScorer'`.** transformers drifted above 4.x.
It is pinned to 4.40.2 for a reason.

**Speaker-latent errors during cloning.** `compute_latents` raises when XTTS
cannot read the reference clips. Check they exist, are 24 kHz mono, and are not
silent.

## Conventions

- Every command is a `just` recipe. Add one rather than documenting a bare
  `uv run` invocation.
- Tunables belong in `config/pipeline.toml`, not in code.
- Stage 4 must stay resumable. A twenty-hour book cannot restart from zero.
- Audio is 24 kHz mono 16-bit PCM everywhere.
- Commit messages stay brief, and carry no `Co-Authored-By` trailer.
- `uv.lock` is committed. Regenerate it with `just relock`, never by hand.
