# Audiobook Factory - one command runner across three isolated Python envs.
#
# Environments never share a virtualenv. whisperx needs pandas>=2.2.3 and
# coqui tts 0.22.0 needs pandas<2.0, so they cannot coexist. `data/` is the
# only bridge between them.
#
# uv manages the interpreter, the virtualenvs and the locks. No pyenv, no poetry.

set shell := ["bash", "-uc"]
set dotenv-load := true

python_version := "3.11.9"

# `just --list` is colourless, and these recipes print a lot. ANSI codes are
# emitted through variables so a recipe stays readable, and NO_COLOR is honoured
# because output is piped into files and CI logs as often as into a terminal.
# Respecting it costs one conditional and is the difference between a readable
# log and one full of escape sequences.
nc := if env_var_or_default("NO_COLOR", "") != "" { "" } else { "\\033[0m" }
bold := if env_var_or_default("NO_COLOR", "") != "" { "" } else { "\\033[1m" }
dim := if env_var_or_default("NO_COLOR", "") != "" { "" } else { "\\033[2m" }
blue := if env_var_or_default("NO_COLOR", "") != "" { "" } else { "\\033[34m" }
green := if env_var_or_default("NO_COLOR", "") != "" { "" } else { "\\033[32m" }
yellow := if env_var_or_default("NO_COLOR", "") != "" { "" } else { "\\033[33m" }
red := if env_var_or_default("NO_COLOR", "") != "" { "" } else { "\\033[31m" }

default:
    @printf '{{bold}}audiobook-factory{{nc}}  {{dim}}just <recipe>  ·  docs/RUNBOOK.md{{nc}}\n\n'
    @just --list --list-heading ''


# ---------------------------------------------------------------- setup ----

# Install the interpreter and all three environments. Run once per machine.
setup: python setup-transcriber setup-bookbinder setup-narrator setup-studio
    @printf '{{green}}{{bold}}all four environments ready{{nc}}  {{dim}}run `just doctor` to verify{{nc}}\n' 

# uv downloads a prebuilt 3.11.9; nothing is compiled.
python:
    uv python install {{python_version}}

setup-transcriber:
    cd apps/transcriber && uv sync

setup-bookbinder:
    cd apps/bookbinder && uv sync

# One pass. Pins live in narrator/pyproject.toml [tool.uv] constraint-dependencies.
setup-narrator:
    # Read the comments beside each pin there before changing any of them.
    cd apps/narrator && uv sync

# Re-resolve from scratch, ignoring the lock. Use after changing a pin.
relock env:
    cd apps/{{env}} && uv lock --upgrade && uv sync

# Swap in CUDA wheels. PC with the RTX 5090 only; skip on Apple Silicon.
gpu-torch env="narrator":
    cd apps/{{env}} && uv pip install torch torchaudio \
      --index-url https://download.pytorch.org/whl/cu124

# --------------------------------------------------------------- checks ----

doctor:
    @printf '{{bold}}host{{nc}}\n'
    @printf '  ffmpeg  %s\n' "$(ffmpeg -version 2>/dev/null | head -1 | cut -d' ' -f3 || echo MISSING)"
    @printf '  uv      %s\n' "$(uv --version | cut -d' ' -f2)"
    @printf '  just    %s\n' "$(just --version | cut -d' ' -f2)"
    @printf '\n{{bold}}environments{{nc}}\n'
    @cd apps/transcriber && uv run python -c \
      "import numpy, pandas, torch; print(f'  {{blue}}transcriber{{nc}}  numpy {numpy.__version__}  pandas {pandas.__version__}  torch {torch.__version__}  cuda {torch.cuda.is_available()}')"
    @cd apps/bookbinder && uv run python -c \
      "import pydantic, ebooklib; print(f'  {{blue}}bookbinder{{nc}}   pydantic {pydantic.__version__}  no torch')"
    @cd apps/narrator && uv run python -c \
      "import numpy, torch, transformers; ok = numpy.__version__.startswith('1.'); \
       print(f'  {{blue}}narrator{{nc}}     numpy {numpy.__version__}  torch {torch.__version__}  transformers {transformers.__version__}  cuda {torch.cuda.is_available()}'); \
       print('' if ok else '  {{red}}numpy must be 1.x here or XTTS fails at inference{{nc}}')"
    @cd apps/studio && uv run python -c \
      "import fastapi; print(f'  {{blue}}studio{{nc}}       fastapi {fastapi.__version__}  no torch')"

# Regenerate docs/schemas/ from the pydantic models.
schemas:
    cd apps/bookbinder && uv run python -m bookbinder.schemas

# Fail if docs/schemas/ has drifted from the models. Runs in CI.
schemas-check:
    cd apps/bookbinder && uv run python -m bookbinder.schemas --check

# Run the test suite in every environment.
test:
    cd apps/bookbinder  && uv run pytest
    cd apps/narrator    && uv run pytest
    cd apps/transcriber && uv run pytest
    cd apps/studio      && uv run pytest

# Type-check every environment against its own installed dependencies.
typecheck:
    cd apps/bookbinder  && uv run pyright
    cd apps/narrator    && uv run pyright
    cd apps/transcriber && uv run pyright
    cd apps/studio      && uv run pyright

# What to run before committing.
check: schemas-check typecheck test
    @printf '{{green}}{{bold}}schemas, types and tests clean{{nc}}\n' 

# Tests for one environment only, with output: just test-one narrator -k formatter
test-one env *args:
    cd apps/{{env}} && uv run pytest -v {{args}}

# Verify the narrator can actually load XTTS-v2, not just import it.
check-narrator:
    cd apps/narrator && COQUI_TOS_AGREED=1 uv run python -c \
      "from narrator.engine import allow_xtts_globals; allow_xtts_globals(); \
       from TTS.api import TTS; TTS('tts_models/multilingual/multi-dataset/xtts_v2'); \
       print('xtts-v2 loads OK')"

# --------------------------------------------- stage 1: clone the voice ----

# What a recording is, and whether it is worth cloning from. ffmpeg only, so
# it answers in seconds rather than after a model download.
probe input:
    cd apps/bookbinder && uv run python -m bookbinder.voices {{quote(absolute_path(input))}}

# 1a. Denoise and normalise a raw recording to 24 kHz mono.
clean input voice:
    ./scripts/preprocess.sh {{quote(input)}} {{quote(voice)}}

# 1b. Cut the cleaned sample on WhisperX boundaries and label it.
label voice device="auto" language="pl":
    cd apps/transcriber && uv run python -m transcriber.auto_label {{quote(voice)}} \
      --device {{quote(device)}} --language {{quote(language)}}

# 1c. Derive speaker latents and render an audition clip.
clone voice device="auto":
    cd apps/narrator && COQUI_TOS_AGREED=1 uv run python -m narrator.clone {{quote(voice)}} \
      --device {{quote(device)}}
    cd apps/studio && uv run python -m studio.catalog_cli voice {{quote(voice)}}

# Optional full fine-tune, CUDA only. batch x accum is the effective batch size.
train voice language="pl" epochs="10" batch="3" accum="84":
    cd apps/narrator && uv run python -m narrator.train {{quote(voice)}} \
      --language {{quote(language)}} --epochs {{quote(epochs)}} \
      --batch-size {{quote(batch)}} --grad-accum {{quote(accum)}}

# Stage 1 end to end.
voice input name language="pl":
    # Judged first: cloning costs minutes and a model download, and most
    # reasons a recording will not work are visible in the file itself.
    just probe {{quote(input)}}
    just clean {{quote(input)}} {{quote(name)}}
    just label {{quote(name)}} auto {{quote(language)}}
    just clone {{quote(name)}}
    # Measured from the audition the clone just made, so every voice in a cast
    # lands on one level rather than whichever the model happened to produce.
    just level {{quote(name)}}

# ----------------------------------------- stages 2-3: text preparation ----

# What books are in a folder, and which are already here. Reads nothing but
# the bytes: the same book under two names is one book, and two different
# books sharing a title get separate names rather than overwriting each other.
scan folder recursive="":
    cd apps/bookbinder && uv run python -m bookbinder.library \
      {{quote(absolute_path(folder))}} {{ if recursive != "" { "--recursive" } else { "" } }}

# Import every book in a folder that is ready. A book whose encoding or
# language cannot be settled pauses on its own and the rest carry on; `language`
# and `encoding` apply to the whole pass, for a folder you already know about.
import-folder folder language="" encoding="" recursive="":
    cd apps/studio && uv run python -m studio.catalog_cli import-folder \
      {{quote(absolute_path(folder))}} --language {{quote(language)}} --encoding {{quote(encoding)}} \
      {{ if recursive != "" { "--recursive" } else { "" } }}

# 2. Parse an ebook into normalised chapters.
ingest source slug="" language="" title="" author="" encoding="":
    cd apps/studio && uv run python -m studio.catalog_cli import-file {{quote(absolute_path(source))}} \
      --slug {{quote(slug)}} --language {{quote(language)}} --title {{quote(title)}} \
      --author {{quote(author)}} --encoding {{quote(encoding)}}

# What ingestion would decide about a file, and on what evidence, without
# writing anything. Use it when a book stops for review.
inspect source encoding="":
    cd apps/bookbinder && uv run python -m bookbinder.ingest {{quote(absolute_path(source))}} \
      --encoding {{quote(encoding)}} --review

# 3. Split chapters into fragments, assigning a cast role to each.
chunk slug voice="" model="":
    cd apps/studio && uv run python -m studio.catalog_cli process {{quote(slug)}} chunk \
      --voice {{quote(voice)}} --model {{quote(model)}}

# The synthesis backends this project knows about, and which one narrates each
# language. Read from config/models.toml.
models:
    cd apps/bookbinder && uv run python -m bookbinder.models

# As above but narrate everything in one voice, ignoring config/cast.yml.
chunk-single slug voice:
    cd apps/studio && uv run python -m studio.catalog_cli process {{quote(slug)}} chunk --voice {{quote(voice)}} --single-voice

# ------------------------------------------------ stages 4-5: the audio ----

# 4. Render every fragment. Resumable: re-run to continue after a crash.
synth slug voice device="auto":
    cd apps/studio && uv run python -m studio.catalog_cli process {{quote(slug)}} synth \
      --voice {{quote(voice)}} --device {{quote(device)}}

# Re-render named fragments, e.g. after a quality check flagged them.
resynth slug chunks:
    cd apps/studio && uv run python -m studio.catalog_cli process {{quote(slug)}} synth --only {{quote(chunks)}}

# Render the first 20 fragments only, to sanity-check the voice.
preview slug voice:
    cd apps/studio && uv run python -m studio.catalog_cli process {{quote(slug)}} synth --voice {{quote(voice)}} --limit 20

# Render to silence at the right durations: structure without models.
dryrun slug strict="":
    cd apps/studio && uv run python -m studio.catalog_cli process {{quote(slug)}} dryrun {{ if strict != "" { "--strict" } else { "" } }}

# 5. Assemble the selected audiobook run.
assemble slug format="":
    cd apps/studio && uv run python -m studio.catalog_cli process {{quote(slug)}} assemble --fmt {{quote(format)}}

# Measure a voice's audition and record the level correction in its profile.
# Re-levelling a voice makes audio already rendered with it out of date.
level voice gain="":
    cd apps/bookbinder && uv run python -m bookbinder.loudness {{quote(voice)}} \
      {{ if gain != "" { "--gain=" + quote(gain) } else { "" } }}

# Is there room for this? Runs on its own, and ahead of synth and assemble.
preflight slug stage="synth" format="":
    cd apps/bookbinder && uv run python -m bookbinder.preflight {{quote(slug)}} \
      {{quote(stage)}} {{quote(format)}}

# 6. Optional: re-transcribe the rendered audio and compare it to the source.
verify slug sample="0":
    cd apps/studio && uv run python -m studio.catalog_cli process {{quote(slug)}} verify --sample {{quote(sample)}}

# Live progress of a running render. Safe to run from another terminal.
progress slug:
    cd apps/studio && uv run python -m studio.catalog_cli progress {{quote(slug)}}

watch slug interval="5":
    cd apps/studio && uv run python -m studio.catalog_cli progress {{quote(slug)}} --watch --interval {{quote(interval)}}

report slug:
    cd apps/studio && uv run python -m studio.catalog_cli progress {{quote(slug)}} --report

# ----------------------------------------------------------- full runs ----

# Stages 2-5 for an already-cloned voice. format: m4b (default) | mp3 | wav.
book source voice slug="" language="" format="":
    just ingest {{quote(source)}} {{quote(slug)}} {{quote(language)}}
    just chunk {{quote(slug)}} {{quote(voice)}}
    just synth {{quote(slug)}} {{quote(voice)}}
    just assemble {{quote(slug)}} {{quote(format)}}

# Ingest, chunk, silence, assemble: the whole structure with no model loaded.
book-dry source slug="" language="":
    just ingest {{quote(source)}} {{quote(slug)}} {{quote(language)}}
    just chunk {{quote(slug)}}
    just dryrun {{quote(slug)}}
    just assemble {{quote(slug)}}

# Everything: clone a voice from a sample, then produce the audiobook.
# The sample may be mp3, wav, m4a or anything ffmpeg reads.
# format: m4b (default) | mp3 | wav.
factory sample voice source slug language="pl" format="":
    just voice {{quote(sample)}} {{quote(voice)}} {{quote(language)}}
    just book {{quote(source)}} {{quote(voice)}} {{quote(slug)}} {{quote(language)}} {{quote(format)}}

# ------------------------------------------------------------- cleanup ----

# Remove finished job records and their logs. Running jobs are left alone.
clean-jobs keep="20":
    cd apps/studio && uv run python -m studio.prune --keep {{quote(keep)}}

# Remove every finished job record.
clean-jobs-all:
    cd apps/studio && uv run python -m studio.prune --all

clean-audio slug:
    rm -rf {{quote("data/audio/" + slug)}}

clean-book slug:
    rm -rf {{quote("data/book/" + slug)}} {{quote("data/audio/" + slug)}}

# ---------------------------------------------------------------- queue ----

# Run the queue from a terminal. The dashboard does this on its own while it
# is open; this is for running a batch without one.
drain:
    cd apps/studio && uv run python -m studio.worker

# One tick of the queue: settle what finished, start at most one thing.
drain-once:
    cd apps/studio && uv run python -m studio.worker --once

# Show the work waiting to happen, grouped by book.
queue book="":
    cd apps/studio && uv run python -m studio.queue --book {{quote(book)}}

# Offer a failed step again. Takes the step number from `just queue`.
queue-retry step:
    cd apps/studio && uv run python -m studio.queue --retry {{quote(step)}}

# Drop a book's remaining steps, or put a paused book back in line.
queue-cancel slug:
    cd apps/studio && uv run python -m studio.queue --cancel {{quote(slug)}}

queue-resume slug:
    cd apps/studio && uv run python -m studio.queue --resume {{quote(slug)}}

# --------------------------------------------------------------- studio ----

# Open the local dashboard. Read-only: it shows books, voices and renders.
ui port="8765":
    cd apps/studio && uv run python -m studio --port {{quote(port)}}

setup-studio:
    cd apps/studio && uv sync

# ------------------------------------------------------------ docker -----
#
# Two profiles. `cpu` runs anywhere including macOS; `gpu` needs an NVIDIA host
# with nvidia-container-toolkit. See docs/ROADMAP-DOCKER.md.

# Build the CPU images: bookbinder and studio. No GPU needed.
docker-build:
    docker compose --profile cpu build

# Build the CUDA images. NVIDIA host only; never built on Apple Silicon.
docker-build-gpu:
    docker compose --profile gpu build

# Run one stage in a container, e.g:
#   just docker-run bookbinder python -m bookbinder.chunk solaris
docker-run service *args:
    docker compose --profile cpu --profile gpu run --rm {{service}} {{args}}

# Ingest, chunk, silence and assemble entirely in containers. No GPU, no models.
docker-smoke slug="dockersmoke" source="data/raw/books/test-book.txt":
    #!/usr/bin/env bash
    set -euo pipefail
    run() { docker compose --profile cpu run --rm bookbinder "$@"; }
    slug={{quote(slug)}}
    source={{quote("/app/" + source)}}
    run python -m bookbinder.ingest "$source" --slug "$slug" --language pl
    run python -m bookbinder.chunk "$slug"
    run python -m bookbinder.dryrun "$slug"
    run python -m bookbinder.assemble "$slug"
    echo "-> data/out/$slug.m4b"

# The dashboard in a container, on http://127.0.0.1:8765
docker-ui:
    docker compose --profile cpu up studio

docker-down:
    docker compose --profile cpu --profile gpu down

# Versioned library and execution records.
catalog-migrate:
    cd apps/studio && uv run python -m studio.catalog_cli migrate

catalog-reconcile:
    cd apps/studio && uv run python -m studio.catalog_cli reconcile

catalog-check:
    cd apps/studio && uv run python -m studio.catalog_cli check

catalog-books:
    cd apps/studio && uv run python -m studio.catalog_cli books

catalog-runs slug="":
    cd apps/studio && uv run python -m studio.catalog_cli runs --slug {{quote(slug)}}

catalog-prepare slug voice="" model="":
    cd apps/studio && uv run python -m studio.catalog_cli prepare {{quote(slug)}} --voice {{quote(voice)}} --model {{quote(model)}}

catalog-stage run action format="":
    cd apps/studio && uv run python -m studio.catalog_cli stage {{quote(run)}} {{quote(action)}} --fmt {{quote(format)}}

# Remove a run, its working directory, and the stored bytes only it held.
catalog-forget run force="":
    cd apps/studio && uv run python -m studio.catalog_cli forget {{quote(run)}} \
      {{ if force != "" { "--force" } else { "" } }}

# Remove a book, every run of it, and its files. There is no undo.
catalog-forget-book slug force="":
    cd apps/studio && uv run python -m studio.catalog_cli forget-book {{quote(slug)}} \
      {{ if force != "" { "--force" } else { "" } }}

# Fold older runs' audio into the assets they duplicate. Safe to re-run.
catalog-collapse:
    cd apps/studio && uv run python -m studio.catalog_cli collapse

# Delete stored bytes nothing points at any more.
catalog-sweep:
    cd apps/studio && uv run python -m studio.catalog_cli sweep

catalog-backup destination:
    cd apps/studio && uv run python -m studio.catalog_cli backup {{quote(absolute_path(destination))}}

catalog-restore source destination:
    cd apps/studio && uv run python -m studio.catalog_cli restore {{quote(absolute_path(source))}} {{quote(absolute_path(destination))}}
