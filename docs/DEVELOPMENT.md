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
just setup     # installs 3.11.9 and all four environments
just doctor    # prints the versions each environment resolved
just check     # schemas + pyright + pytest, ~8 s
```

Budget roughly 2.7 GB of virtualenvs plus 1.7 GB of model weights on first
synthesis.

| Path | Size | Notes |
|---|---|---|
| `apps/narrator/.venv` | 1.6 GB | 149 packages, torch dominates |
| `apps/transcriber/.venv` | 1.1 GB | 109 packages |
| `apps/bookbinder/.venv` | 86 MB | 30 packages, no ML |
| `apps/studio/.venv` | 92 MB | the dashboard; fastapi, no ML |
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

The contract is `apps/bookbinder/src/bookbinder/manifest.py`. If you change the
chunk schema there, update both consumers: `narrator/synth.py` writes
`audio_path` and `duration_sec` into it, and `bookbinder/assemble.py` reads
them back.

## Everyday commands

Full reference: [COMMANDS.md](COMMANDS.md). The ones that matter while working:

```bash
just                      # list every recipe
just check                # schemas + types + tests; run before committing
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

`apps/bookbinder/src/bookbinder/manifest.py` holds pydantic models for everything
that crosses an environment boundary. `just schemas` exports them to
`docs/schemas/`, and `just schemas-check` fails if the exported files have
drifted. Both run as part of `just check` and in CI.

narrator and transcriber cannot import those models, since they resolve a
different numpy. They mirror the shape by hand and write plain JSON. That is the
one place a silent divergence can appear, so `apps/bookbinder/tests/test_schemas.py`
validates a copy of exactly what each of them writes. If you change a model,
update the writer and that fixture together.

## Adding a dependency

Always inside the environment that needs it, never at the repo root:

```bash
cd apps/bookbinder && uv add ebooklib
cd narrator   && uv add --dev pytest-cov
```

**The narrator is different.** Its `[tool.uv] constraint-dependencies` block is
what keeps XTTS working. Before adding anything there, check whether the new
package drags numpy above 2.0:

```bash
cd apps/narrator && uv add <package> && uv run python -c "import numpy; print(numpy.__version__)"
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

Most of the 356 tests are in bookbinder and studio, because that is where the
logic that can silently corrupt a book sits: chunking, role assignment, assembly,
and the layer that runs commands. The pipeline is covered end to end in
bookbinder too, with the dry-run renderer standing in for synthesis, so no model
weights are involved.

Studio's tests include structural checks on the templates. A page rendering with
a 200 and containing the right words does not prove the content landed in the
right place: a bad edit once put the job table inside the page header and every
test still passed.

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
| `check` | Matrix over the four environments: sync, schema drift (bookbinder), pyright, pytest |

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

**`Could not load libtorchcodec`.** torchcodec ships a compiled extension tied
to one torch ABI, and its decoders link against a specific FFmpeg range. The two
environments solve this in opposite directions, which is fine because they are
separate virtualenvs.

The narrator pins torch 2.8, because torchaudio 2.9 dropped the native backends
and routes through torchcodec, which then cannot find Homebrew's FFmpeg from
uv's standalone CPython.

The transcriber pins torch 2.14, because pyannote-audio requires torchcodec and
only recent builds support FFmpeg 9. whisperx declares `torch<2.9`, so that is
lifted with `override-dependencies`. If you change torch in either, run the real
thing, not just the tests: `just label` exercises alignment and `just verify`
exercises decoding.

**`UnpicklingError` loading a checkpoint.** PyTorch 2.6 defaults `torch.load`
to `weights_only=True`. `narrator.engine.allow_xtts_globals()` allowlists the
XTTS config classes; call it before loading any XTTS checkpoint. Do not reach
for `weights_only=False`, which disables the protection process-wide.

**`cannot import name 'BeamSearchScorer'`.** transformers drifted above 4.x.
It is pinned to 4.40.2 for a reason.

**Speaker-latent errors during cloning.** `compute_latents` raises when XTTS
cannot read the reference clips. Check they exist, are 24 kHz mono, and are not
silent.

**A finished audiobook of the right length that plays as silence.** A dry run
fills `data/audio/<slug>/` with a wav per fragment, at the estimated duration
and under the name a real render would use. Stage 4 resumes by skipping any
fragment that already has audio, so a render started on top of a dry run adopts
all of it, finishes in under a second and reports success.

Three things now stand in the way, and all three matter because each covers a
case the others miss:

- `bookbinder.dryrun` writes `.dry-run.json` beside the silence, before the
  first wav rather than after the last, so an interrupted dry run is marked too.
- `narrator.synth.discard_dry_run` deletes marked silence before the resume
  logic sees it. Without the marker it deletes nothing, which is what keeps it
  from ever touching a real render.
- `bookbinder.assemble.all_silent` samples twelve fragments and warns when they
  are all silent. This is the one that does not depend on knowing the cause, so
  it also catches audio from before the marker existed and a voice that
  rendered to nothing.

`just verify` refuses outright on marked silence, before whisperx is imported.

**Fragments re-rendered as stale, or an assembly refused for "different
text".** Resume used to mean "skip anything that already has a wav", which is
what makes a twenty-hour book survivable and also how a book comes out in the
wrong voice: change the model, re-run, and every existing fragment is kept
while the report says it succeeded.

Each fragment now carries a fingerprint over the text, language, model
identity, voice, voice revision and effective settings behind it. Stage 4
reuses a wav only when that matches and the file still decodes, and appends to
`data/audio/<slug>/fingerprints.jsonl` as each fragment lands, so a render
killed at hour six leaves the first six hours reusable. Assembly recomputes the
same fingerprint from the current plan and refuses anything that disagrees.

Re-cloning a voice counts as a change, because the latents are hashed into the
revision. Audio rendered before any of this exists carries no fingerprint and
is let through, rather than invalidating every book already on disk. The
algorithm lives in two places, `bookbinder/fingerprint.py` and its mirror in
`narrator/`, both stdlib-only so a test can load both and compare them.

**`'<slug>' is not ready to assemble`.** Stage 5 now checks the rendered
fragments against the chunk plan before it runs ffmpeg, and names what is
wrong: fragments that were never rendered, a wav that has been deleted, a
duplicate, or ids that are absent from the plan because the book was re-chunked
after the render. It used to print one line per missing fragment to stderr and
assemble the book anyway, producing a shorter audiobook whose chapter marks
were all past their real positions. Finish the render with `just synth`, or
repair named fragments with `just resynth`.

**A book the dashboard calls `failed` that has a file in `data/out/`.** That is
correct, and it is the point: a render that fails leaves the previous export in
place, so the presence of a file says nothing about the current state. The
render report decides, and `failed` outranks `done`.

**Adding a synthesis backend.** Nothing about a model is compiled in any more.
`config/models.toml` records which languages it narrates, which a voice
reference may be in, its fragment limit, its native rate, the controls it
implements and the environment that can load it. `just models` prints it.

Three things follow from that. Chunking resolves the backend before it splits
anything, because the fragment size is the model's property and not the
language's, so changing model means re-chunking rather than patching fragments.
The resolved choice is snapshotted into `book.json`, so changing a default
tomorrow cannot change a book already queued. And stage 4 reads that block
rather than deciding for itself: a book bound to an engine the running
environment does not implement is refused, never rendered with whatever is
installed.

The code side is `narrator/backends/`. A backend answers two questions, what to
speak and at what rate the audio comes back. If its dependencies conflict with
the XTTS pins, it gets its own environment under `apps/` and its own module
there; see `docs/DECISIONS.md`.

**A book that stops with `needs review` at ingestion.** Two things are now
established from the file rather than assumed: the encoding of plain text, and
the language of the book. Either can be genuinely undecidable, and guessing is
worse than asking. `just inspect <file>` prints the evidence. Resolve it with
`just ingest <file> "" <language>` or the `encoding` argument.

The usual causes are a file too short to classify, a passage in a third
language, and two single-byte encodings that both read the file but disagree
about the text. Windows-1250 and ISO-8859-2 are the pair that matters for
Polish: both decode any byte without error, so only the resulting text tells
them apart, and `charset-normalizer` alone gets it wrong. The decision comes
from scoring each candidate's text, with the detector used only to order
otherwise-equal readings.

**The narration says something the page does not.** Three representations of
every book are kept, and they are meant to differ. The staged source under
`data/sources/` is what arrived. Each chunk's `source_text` is the printed
spelling. Its `text` is what the model reads, with abbreviations expanded and a
few symbols spoken. `substitutions` records every change with its span on both
sides, so a fragment can be read in either direction and quality checking does
not report "doktor" as a mismatch against "dr.".

Numbers are deliberately left as digits. Polish inflects numerals for case and
gender, so one fixed expansion of "3" is wrong more often than the digits are.
Settle individual cases in `data/book/<slug>/pronunciation.yml`, a mapping of
written form to spoken form that wins over every built-in rule. Whether the
models read digits well enough is a question for slice B.

**A `busy` refusal from the dashboard.** No two jobs run on one book at a time,
whether or not they take the render lock. Chunking during a render rewrites the
manifest underneath it, assembling reads a fragment list still being appended
to, and verifying reads audio mid-write. Wait for the running job or cancel it.

## Conventions

- Every command is a `just` recipe. Add one rather than documenting a bare
  `uv run` invocation.
- Tunables belong in `config/pipeline.toml`, not in code.
- Stage 4 must stay resumable. A twenty-hour book cannot restart from zero.
- Audio is 24 kHz mono 16-bit PCM everywhere.
- Commit messages stay brief, and carry no `Co-Authored-By` trailer.
- `uv.lock` is committed. Regenerate it with `just relock`, never by hand.
