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

default:
    @just --list

# ---------------------------------------------------------------- setup ----

# Install the interpreter and all three environments. Run once per machine.
setup: python setup-transcriber setup-bookbinder setup-narrator
    @echo "all environments ready - run 'just doctor' to verify"

# uv downloads a prebuilt 3.11.9; nothing is compiled.
python:
    uv python install {{python_version}}

setup-transcriber:
    cd transcriber && uv sync

setup-bookbinder:
    cd bookbinder && uv sync

# One pass. The pins that keep XTTS alive live in narrator/pyproject.toml
# under [tool.uv] constraint-dependencies - read the comments there before
# touching any of them.
setup-narrator:
    cd narrator && uv sync

# Re-resolve from scratch, ignoring the lock. Use after changing a pin.
relock env:
    cd {{env}} && uv lock --upgrade && uv sync

# Swap in CUDA wheels. PC with the RTX 5090 only; skip on Apple Silicon.
gpu-torch env="narrator":
    cd {{env}} && uv pip install torch torchaudio \
      --index-url https://download.pytorch.org/whl/cu124

# --------------------------------------------------------------- checks ----

doctor:
    @echo "== ffmpeg ==" && (ffmpeg -version | head -1 || echo MISSING)
    @echo "== uv ==" && uv --version
    @echo "== transcriber ==" && cd transcriber && uv run python -c \
      "import numpy, pandas, whisperx, torch; print('numpy', numpy.__version__, '| pandas', pandas.__version__, '| torch', torch.__version__, '| cuda', torch.cuda.is_available())"
    @echo "== bookbinder ==" && cd bookbinder && uv run python -c \
      "import ebooklib, pysbd, pypdf; print('ok')"
    @echo "== narrator ==" && cd narrator && uv run python -c \
      "import numpy, torch, transformers; print('numpy', numpy.__version__, '| torch', torch.__version__, '| transformers', transformers.__version__, '| cuda', torch.cuda.is_available())"

# Run the test suite in every environment.
test:
    cd bookbinder  && uv run pytest
    cd narrator    && uv run pytest
    cd transcriber && uv run pytest

# Type-check every environment against its own installed dependencies.
typecheck:
    cd bookbinder  && uv run pyright
    cd narrator    && uv run pyright
    cd transcriber && uv run pyright

# What to run before committing.
check: typecheck test
    @echo "types and tests clean"

# Tests for one environment only, with output: just test-one narrator -k formatter
test-one env *args:
    cd {{env}} && uv run pytest -v {{args}}

# Verify the narrator can actually load XTTS-v2, not just import it.
check-narrator:
    cd narrator && COQUI_TOS_AGREED=1 uv run python -c \
      "from narrator.engine import allow_xtts_globals; allow_xtts_globals(); \
       from TTS.api import TTS; TTS('tts_models/multilingual/multi-dataset/xtts_v2'); \
       print('xtts-v2 loads OK')"

# --------------------------------------------- stage 1: clone the voice ----

# 1a. Denoise and normalise a raw recording to 24 kHz mono.
clean input voice:
    ./scripts/preprocess.sh "{{input}}" "{{voice}}"

# 1b. Cut the cleaned sample on WhisperX boundaries and label it.
label voice device="auto" language="pl":
    cd transcriber && uv run python -m transcriber.auto_label "{{voice}}" \
      --device {{device}} --language {{language}}

# 1c. Derive speaker latents and render an audition clip.
clone voice device="auto":
    cd narrator && COQUI_TOS_AGREED=1 uv run python -m narrator.clone "{{voice}}" \
      --device {{device}}

# Optional: full fine-tune. Needs CUDA; instant cloning covers most books.
# batch x accum is the effective batch size - keep the product near 250.
train voice language="pl" epochs="10" batch="3" accum="84":
    cd narrator && uv run python -m narrator.train "{{voice}}" \
      --language {{language}} --epochs {{epochs}} \
      --batch-size {{batch}} --grad-accum {{accum}}

# Stage 1 end to end.
voice input name language="pl":
    just clean "{{input}}" "{{name}}"
    just label "{{name}}" auto "{{language}}"
    just clone "{{name}}"

# ----------------------------------------- stages 2-3: text preparation ----

# 2. Parse an ebook into normalised chapters.
ingest source slug="" language="":
    cd bookbinder && uv run python -m bookbinder.ingest "../{{source}}" \
      {{ if slug != "" { "--slug " + slug } else { "" } }} \
      {{ if language != "" { "--language " + language } else { "" } }}

# 3. Split chapters into model-sized fragments with metadata.
chunk slug voice="":
    cd bookbinder && uv run python -m bookbinder.chunk "{{slug}}" \
      {{ if voice != "" { "--voice " + voice } else { "" } }}

# ------------------------------------------------ stages 4-5: the audio ----

# 4. Render every fragment. Resumable: re-run to continue after a crash.
synth slug voice device="auto":
    cd narrator && COQUI_TOS_AGREED=1 uv run python -m narrator.synth "{{slug}}" \
      --voice "{{voice}}" --device {{device}}

# Render the first 20 fragments only, to sanity-check the voice.
preview slug voice:
    cd narrator && COQUI_TOS_AGREED=1 uv run python -m narrator.synth "{{slug}}" \
      --voice "{{voice}}" --limit 20

# 5. Mux fragments, pauses and chapter marks into the finished audiobook.
assemble slug format="":
    cd bookbinder && uv run python -m bookbinder.assemble "{{slug}}" \
      {{ if format != "" { "--fmt " + format } else { "" } }}

# ----------------------------------------------------------- full runs ----

# Stages 2-5 for an already-cloned voice.
book source voice slug="" language="":
    just ingest "{{source}}" "{{slug}}" "{{language}}"
    just chunk "{{slug}}" "{{voice}}"
    just synth "{{slug}}" "{{voice}}"
    just assemble "{{slug}}"

# Everything: clone a voice from a sample, then produce the audiobook.
factory sample voice source slug language="pl":
    just voice "{{sample}}" "{{voice}}" "{{language}}"
    just book "{{source}}" "{{voice}}" "{{slug}}" "{{language}}"

# ------------------------------------------------------------- cleanup ----

clean-audio slug:
    rm -rf "data/audio/{{slug}}"

clean-book slug:
    rm -rf "data/book/{{slug}}" "data/audio/{{slug}}"

# ------------------------------------------------------------ docker -----

up service:
    docker compose run --rm {{service}}

build-images:
    docker compose build
