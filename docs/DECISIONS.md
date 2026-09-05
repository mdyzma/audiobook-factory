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

`poetry add tts==0.22.0` cannot resolve. The working sequence, encoded in
`just setup-narrator-extras`:

1. `pip install "TTS==0.22.0" --no-deps`
2. Install its real runtime dependencies by hand, including `gruut[all]==2.2.3`
   with `--no-deps --force-reinstall`.
3. Install the spaCy compiled core (`thinc`, `blis`, `preshed`, `murmurhash`,
   `catalogue`, `confection`, `srsly`, `wasabi`) with `--no-deps`, since pip will
   otherwise upgrade numpy underneath it.
4. Reinstall `numpy<2.0.0` last. Any later install that touches numpy undoes this.

On Apple Silicon `blis` and `thinc` need `setuptools` and `cython` present before
they will build.

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
