# Decisions carried over from the Gemini session

Extracted from `gemini-session.md`. These are the conclusions that cost the most
time to reach, so they are recorded here rather than rediscovered.

## The conflict that shapes the whole project

`whisperx==3.8.1` requires `pandas>=2.2.3`. `tts==0.22.0` requires `pandas>=1.4,<2.0`.
There is no resolution. Poetry will fail, and forcing it produces an environment
where XTTS raises `AttributeError` at inference because numpy 2.x changed its API.

Everything else follows from this: separate environments, `data/` as the only bridge.

## Environments

| Env | Purpose | numpy | pandas | Never contains |
|---|---|---|---|---|
| `transcriber` | WhisperX auto-labelling, QA | >=2.1 | >=2.2.3 | `tts` |
| `bookbinder` | Ebook parsing, chunking, muxing | any | none | torch |
| `narrator` | XTTS-v2 cloning, synthesis, training | <2.0 (1.26.4) | <2.0 | `whisperx` |

Python is pinned to 3.11.9 everywhere. Coqui TTS does not work on 3.12+, and
`requires-python = ">=3.11,<3.12"` is what stops Poetry reaching for a newer one.

## Installing the narrator environment

Under Poetry this environment could not be resolved at all. The workaround was
`pip install TTS --no-deps` followed by hand-listing its dependencies in a
load-bearing order, with numpy forced back to 1.x as the final step.

uv resolves it in a single `uv sync`. The pins live in `narrator/pyproject.toml`
under `[tool.uv] constraint-dependencies`, which bounds the transitive graph
without restating it. Nothing has to be installed in a particular order, and the
result is captured in `uv.lock`.

## Audio format

XTTS-v2 expects 24 kHz mono 16-bit PCM. WhisperX prefers 16 kHz but resamples on
its own, so 24 kHz is the single normalised format for the whole pipeline.

Clean before cutting: `afftdn` for hiss, `loudnorm` for level. A clone trained on
noisy audio reproduces the noise, which is the usual failure of amateur voice clones.

## Segment selection

Cut on WhisperX alignment boundaries, not fixed intervals. Fixed 10-second cuts
clip word endings, and clipped endings are what make a clone sound metallic.
Keep segments between 1.5 and 15 seconds; XTTS is happiest around 2 to 12.

## Cloning modes

- **Instant**: speaker latents averaged from a dozen reference clips. Minutes,
  works on CPU and MPS. Good enough for most books.
- **Fine-tune**: full XTTS training. Learns the speaker's own pauses and
  intonation. Needs CUDA, roughly an hour on a 32 GB card with a large batch and
  mixed precision. Not possible on Apple Silicon.

Collect 30 to 60 minutes of clean recording before attempting a fine-tune.

Fine-tuning is implemented in `narrator/src/narrator/train.py`. It starts from
the XTTS v2.0.2 base files, which it caches under `training/base/`, reads the
auto-labeller's `metadata.csv` through a local formatter, and repoints the voice
profile at the resulting checkpoint when it finishes. Effective batch size is
`batch_size * grad_accum`; keep the product near 250 and lower `max_audio_sec`
first if VRAM runs short, since cost scales with the square of clip length.

## Hardware split

The two stages are independent, so they can run on different machines. Labelling
on the M1 while travelling, synthesis on the CUDA box afterwards, with the JSON
and CSV files carried between them.

Docker on Apple Silicon has no GPU passthrough. On the Mac, use the native Poetry
environments; the compose file is for the CUDA host, which also needs
`nvidia-container-toolkit`.

## Discarded

The session also explored a `modern-speech` environment for Qwen-TTS and
Fish-Speech, with `transformers==4.57.3`, `numpy==1.26.4`, `pydantic==2.9.2` on
Python 3.11.9. It is not part of this pipeline. Add it as a fourth sibling
directory if those models are ever wanted; the `data/` contract would not change.

---

# Addenda from the first real install (2026-09-05, MacBook M1)

The session transcript predates several upstream changes. These four broke the
narrator environment and are now fixed in `justfile` and `narrator/constraints.txt`.

## Poetry cannot install TTS, but pip can, given constraints

The session's answer was `pip install TTS --no-deps` followed by hand-listing
dependencies. That leaves gaps, and each missing module only surfaces at import.

Better: install TTS *with* its dependencies under `narrator/constraints.txt`.
The constraints file holds numpy, pandas and the spaCy stack, so pip resolves
everything else itself without lifting numpy. Never drop the `-c` flag.

## The spaCy stack must stay on 3.7

`spacy` 3.8 requires `thinc` 8.3, which requires numpy >= 2.0. That is
incompatible with the narrator by definition. Pinned: spacy 3.7.5, thinc 8.2.5,
blis 0.7.11, confection 0.1.5.

## transformers 5.x removed BeamSearchScorer

`TTS/tts/layers/xtts/stream_generator.py` imports it. Pinned transformers to
4.40.2, which forces tokenizers 0.19.1 and huggingface-hub 0.23.5.

## PyTorch 2.6 broke XTTS checkpoint loading

`torch.load` now defaults to `weights_only=True`, and XTTS checkpoints pickle
their config objects, so loading raises `UnpicklingError`.

Fixed in `narrator/src/narrator/engine.py:allow_xtts_globals()`, which allowlists
`XttsConfig`, `XttsAudioConfig`, `XttsArgs` and `BaseDatasetConfig`. Deliberately
*not* fixed by setting `weights_only=False`, which would disable arbitrary-code
protection for every checkpoint the process loads.

## torchaudio 2.11 needs torchcodec

Audio loading now routes through `torchcodec`, which is not a declared TTS
dependency. Installed explicitly.

## Versions that actually work

| | transcriber | narrator |
|---|---|---|
| python | 3.11.9 | 3.11.9 |
| numpy | 2.4.6 | 1.26.4 |
| pandas | 3.0.5 | 1.5.3 |
| torch | 2.8.0 | 2.14.0 |
| transformers | 4.57.6 | 4.40.2 |

Verified on Apple Silicon with MPS. Synthesis ran at 0.8x realtime on the M1,
so a ten-hour audiobook takes roughly twelve hours there. This is the workload
that belongs on the CUDA machine.


---

# Addendum: migration to uv (2026-09-06)

pyenv and Poetry are gone. uv manages the interpreter, the virtualenvs and the
locks for all three environments.

## What this fixed

The hand-ordered pip sequence in the narrator is gone. `constraint-dependencies`
expresses the same pins declaratively, so `uv sync` resolves TTS with its full
dependency tree in one pass. There is no longer an install step whose position
in the sequence matters.

Poetry itself was also a liability here: its virtualenv was built against a
Homebrew Python, and a routine `brew upgrade` deleted that interpreter and broke
every `poetry` invocation. uv's Python is self-contained and unaffected by brew.

Installing 3.11.9 took uv about three seconds against several minutes for
pyenv's source build.

The three projects are now real packages built by hatchling, so `python -m` finds
them without a `PYTHONPATH=src` prefix, and the `sys.path` shims are removed.

## torch is pinned to 2.8.0 in the narrator

torchaudio 2.9 dropped its native backends and routes audio loading through
`torchcodec`, whose bundled library resolves FFmpeg's shared objects at runtime.
uv's standalone CPython carries no Homebrew rpath, so that lookup fails on macOS
with "Could not load libtorchcodec".

Pinning torch and torchaudio to 2.8.0 keeps the soundfile backend and removes the
native dependency. This also aligns the narrator with the transcriber's torch.

The pyenv build did not hit this, because its Homebrew-linked interpreter could
find `/opt/homebrew/lib` on its own. That made the old setup quietly
host-dependent; the uv one is not.

## Locks are committed

`uv.lock` is tracked for all three environments, which is what makes the CUDA
machine install the same versions verified here. Regenerate with
`just relock <env>`, never by hand.

---

# Addendum: casting, dry runs and contracts (2026-09-06)

Five gaps closed after comparing this against an earlier monorepo attempt.

## Roles are typographic, not semantic

Dialogue is detected from quotation marks, dashes and explicit `Speaker:`
labels. No model, no parsing of meaning.

The bias is deliberate: narration read in a character's voice is far more
jarring than dialogue left with the narrator, so ambiguous cases stay with the
narrator. `— Witaj — powiedziała.` is mostly a narration tag, so a short line
containing an attribution verb goes to the narrator despite opening with a dash.
`Uwaga: ...` must not become a character called `uwaga`, so a speaker label
counts only when the name is in the cast or the rest of the line also looks like
dialogue.

A named speaker who is not in the cast falls back to the generic dialogue voice
rather than inventing one, and an unknown role falls back to the narrator rather
than refusing to render.

## Speaker latents are pooled per voice

XTTS weights are shared across voices; only the latents differ. `VoicePool`
loads the model once and caches a latent pair per voice, so a three-voice book
costs three derivations rather than three per chunk.

## Dry runs are the fast path

Silence at each fragment's estimated duration, assembled the same way as real
audio. Catches chapter marks in the wrong place, pauses that do not add up and
uncast roles, in seconds instead of hours, in the environment with no torch.
It is also what lets CI cover the pipeline end to end.

Uncloned voices are a note by default and a failure under `--strict`, because
the usual reason to dry-run is to check structure *before* cloning anything.

## Chunks are strict, reports are not

The manifest models validate on write, so a malformed chunk fails immediately
rather than thousands of fragments into a render, and `read_chunks` names the
file and line when something is wrong.

Chunks forbid unknown fields, since bookbinder authors them and a typo is a bug.
Reports ignore them, because narrator and transcriber write those by hand in
environments that cannot import the models, and they include derived summary
values. Rejecting those would make a report unreadable by its own schema. That
divergence was real: the narrator's report was missing `realtime_factor` and
`ok` until a cross-environment fixture test caught it.

`just schemas-check` guards the rest.

## Quality is measured by re-transcription

XTTS truncates, skips and repeats without raising anything. The only signal is
the audio disagreeing with the text, so `just verify` reads the render back with
WhisperX and reports word error rate.

Punctuation and case are stripped before comparing, and Unicode is normalised to
composed form. Diacritics are kept: `ą` and `a` are different words in Polish,
and folding them would hide a real mispronunciation.

Expect a non-zero baseline, because the transcriber mishears too. The outliers
are the point, and `--sample` makes a long book affordable to check.
