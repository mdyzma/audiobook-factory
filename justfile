# Audiobook Factory - one command runner across three isolated Python envs.
#
# Environments never share a virtualenv. whisperx needs pandas>=2.2.3 and
# coqui tts 0.22.0 needs pandas<2.0, so they cannot coexist. `data/` is the
# only bridge between them.

set shell := ["bash", "-uc"]
set dotenv-load := true

python_version := "3.11.9"
root := justfile_directory()

default:
    @just --list

# ---------------------------------------------------------------- setup ----

# Install every environment. Run once per machine.
setup: setup-transcriber setup-bookbinder setup-narrator
    @echo "all environments ready - run 'just doctor' to verify"

setup-transcriber:
    cd transcriber && pyenv local {{python_version}} \
      && poetry env use $(pyenv which python) \
      && poetry lock && poetry install

setup-bookbinder:
    cd bookbinder && pyenv local {{python_version}} \
      && poetry env use $(pyenv which python) \
      && poetry lock && poetry install

# Install the Coqui TTS environment. See setup-narrator-extras for why it is odd.
setup-narrator: _narrator-base setup-narrator-extras
    @echo "narrator ready"

_narrator-base:
    cd narrator && pyenv local {{python_version}} \
      && poetry env use $(pyenv which python) \
      && poetry lock && poetry install

# Install TTS under narrator/constraints.txt, which holds numpy/spaCy/transformers.
setup-narrator-extras:
    # Every install here passes -c constraints.txt. That file is what stops pip
    # lifting numpy to 2.x or spaCy to a thinc that requires it. Do not drop it.
    cd narrator && poetry run pip install -c constraints.txt "TTS==0.22.0"
    cd narrator && poetry run pip install -c constraints.txt \
      "transformers==4.40.2" "tokenizers==0.19.1" "huggingface-hub==0.23.5" \
      "jaraco.context" "jaraco.functools"
    cd narrator && poetry run pip install -c constraints.txt "numpy==1.26.4" --force-reinstall
    @echo "numpy pinned back to 1.26.4 - this must stay the last install step"

# Verify the narrator can actually load XTTS-v2, not just import it.
check-narrator:
    cd narrator && COQUI_TOS_AGREED=1 PYTHONPATH=src poetry run python -c \
      "from narrator.engine import allow_xtts_globals; allow_xtts_globals(); \
       from TTS.api import TTS; TTS('tts_models/multilingual/multi-dataset/xtts_v2'); \
       print('xtts-v2 loads OK')"

# Swap in CUDA wheels. PC with the RTX 5090 only; skip on Apple Silicon.
gpu-torch env="narrator":
    cd {{env}} && poetry run pip install torch torchaudio \
      --index-url https://download.pytorch.org/whl/cu124 --force-reinstall

# --------------------------------------------------------------- checks ----

doctor:
    @echo "== ffmpeg ==" && (ffmpeg -version | head -1 || echo MISSING)
    @echo "== transcriber ==" && cd transcriber && PYTHONPATH=src poetry run python -c \
      "import numpy, pandas, whisperx, torch; print('numpy', numpy.__version__, '| pandas', pandas.__version__, '| torch', torch.__version__, '| cuda', torch.cuda.is_available())"
    @echo "== bookbinder ==" && cd bookbinder && PYTHONPATH=src poetry run python -c \
      "import ebooklib, pysbd, pypdf; print('ok')"
    @echo "== narrator ==" && cd narrator && PYTHONPATH=src poetry run python -c \
      "import numpy, torch; from TTS.api import TTS; print('numpy', numpy.__version__, '| torch', torch.__version__, '| cuda', torch.cuda.is_available())"

# --------------------------------------------- stage 1: clone the voice ----

# 1a. Denoise and normalise a raw recording to 24 kHz mono.
clean input voice:
    ./scripts/preprocess.sh "{{input}}" "{{voice}}"

# 1b. Cut the cleaned sample on WhisperX boundaries and label it.
label voice device="auto" language="pl":
    cd transcriber && PYTHONPATH=src poetry run python -m transcriber.auto_label "{{voice}}" \
      --device {{device}} --language {{language}}

# 1c. Derive speaker latents and render an audition clip.
clone voice device="auto":
    cd narrator && PYTHONPATH=src poetry run python -m narrator.clone "{{voice}}" --device {{device}}

# Optional: full fine-tune. Needs CUDA.
train voice epochs="10" batch="32":
    cd narrator && PYTHONPATH=src poetry run python -m narrator.train "{{voice}}" \
      --epochs {{epochs}} --batch-size {{batch}}

# Stage 1 end to end.
voice input name language="pl":
    just clean "{{input}}" "{{name}}"
    just label "{{name}}" auto "{{language}}"
    just clone "{{name}}"

# ----------------------------------------- stages 2-3: text preparation ----

# 2. Parse an ebook into normalised chapters.
ingest source slug="" language="":
    cd bookbinder && PYTHONPATH=src poetry run python -m bookbinder.ingest "../{{source}}" \
      {{ if slug != "" { "--slug " + slug } else { "" } }} \
      {{ if language != "" { "--language " + language } else { "" } }}

# 3. Split chapters into model-sized fragments with metadata.
chunk slug voice="":
    cd bookbinder && PYTHONPATH=src poetry run python -m bookbinder.chunk "{{slug}}" \
      {{ if voice != "" { "--voice " + voice } else { "" } }}

# ------------------------------------------------ stages 4-5: the audio ----

# 4. Render every fragment. Resumable: re-run to continue after a crash.
synth slug voice device="auto":
    cd narrator && PYTHONPATH=src poetry run python -m narrator.synth "{{slug}}" \
      --voice "{{voice}}" --device {{device}}

# Render the first 20 fragments only, to sanity-check the voice.
preview slug voice:
    cd narrator && PYTHONPATH=src poetry run python -m narrator.synth "{{slug}}" \
      --voice "{{voice}}" --limit 20

# 5. Mux fragments, pauses and chapter marks into the finished audiobook.
assemble slug format="":
    cd bookbinder && PYTHONPATH=src poetry run python -m bookbinder.assemble "{{slug}}" \
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
