# Command reference

For the one-command path, see `bin/audiobook --help`: it takes a voice sample,
an ebook and an output location, and runs everything below in order.

The rest of this file covers the individual stages. Every one is a `just`
recipe, and `just` on its own lists them.

Arguments are shown as `name` when required and `name="default"` when optional.
Trailing optional arguments can be omitted; to skip one and set a later one,
pass `""` for the ones between.

## Setup

| Command | What it does |
|---|---|
| `just setup` | Installs Python 3.11.9 and all three environments. Run once per machine. |
| `just python` | Installs the interpreter only. uv downloads a prebuilt build; nothing compiles. |
| `just setup-bookbinder` | Syncs the ebook and assembly environment. |
| `just setup-transcriber` | Syncs the WhisperX environment. |
| `just setup-studio` | Syncs the dashboard environment. |
| `just setup-narrator` | Syncs the XTTS environment in one pass. Pins live in its `[tool.uv] constraint-dependencies`. |
| `just relock <env>` | Re-resolves from scratch, ignoring the lock. Use after changing a pin. |
| `just gpu-torch <env="narrator">` | Swaps in CUDA 12.4 wheels. CUDA host only; skip on Apple Silicon. |

## Checks

| Command | What it does |
|---|---|
| `just check` | Types and tests across all three environments. Run before committing. |
| `just test` | pytest in every environment. About 3 seconds. |
| `just typecheck` | pyright in every environment, each against its own dependencies. About 4 seconds. |
| `just test-one <env> [args]` | One environment, verbose. Example: `just test-one narrator -k formatter`. |
| `just doctor` | Prints ffmpeg, uv and the versions each environment resolved. |
| `just check-narrator` | Loads XTTS-v2 for real, not just imports it. Downloads weights on first run. |
| `just models` | Lists the synthesis backends and which one narrates each language. |
| `just schemas` | Regenerates `docs/schemas/` from the pydantic models. |
| `just schemas-check` | Fails if the exported schemas have drifted. Part of `just check` and CI. |

## Stage 1: clone a voice

| Command | What it does |
|---|---|
| `just probe <input>` | What a recording is, and whether it is worth cloning from: duration, level, silence, channels. ffmpeg only, so it answers in seconds. Exits non-zero on a recording that will not work. |
| `just clean <input> <voice>` | Denoises and normalises a recording to 24 kHz mono. |
| `just label <voice> <device="auto"> <language="pl">` | Cuts the sample on WhisperX alignment boundaries and transcribes each piece. |
| `just clone <voice> <device="auto">` | Derives speaker latents and renders an audition clip to judge the clone. |
| `just train <voice> <language="pl"> <epochs="10"> <batch="3"> <accum="84">` | Optional full fine-tune. CUDA only. `batch * accum` is the effective batch size; keep it near 250. |
| `just voice <input> <name> <language="pl">` | Stage 1 end to end: probe, clean, label, clone. Stops before the model download if the recording cannot work. The dashboard's **Create voice** button runs this. |

## Stages 2 and 3: prepare the text

| Command | What it does |
|---|---|
| `just scan <folder> [recursive]` | Lists the books in a folder and says what each one is: new, already imported, a revision of one that is here, a duplicate, or an unsupported file. Reads the bytes, so the same book under two names is one book. Non-recursive unless asked. |
| `just import-folder <folder> <language=""> <encoding=""> [recursive]` | Imports every book in a folder that is ready. A book whose encoding or language cannot be settled pauses on its own and the rest carry on. `language` and `encoding` apply to the whole pass. Exits non-zero when anything paused. |
| `just inspect <source> <encoding="">` | Reports the encoding and language ingestion would choose, and the evidence for each, without writing anything. Use it when a book stops for review. |
| `just ingest <source> <slug=""> <language=""> <title=""> <author=""> <encoding="">` | Parses an EPUB, PDF or text file into normalised chapters, establishing the file's encoding and the book's language rather than assuming them. Plain text carries no metadata, so pass `title` and `author` or they come from the filename. Setting them later by editing `book.json` does not last: chunking rebuilds it. Pass `language` (`pl` or `en`) or `encoding` (`cp1250`, `iso-8859-2`) to decide either yourself; a book that cannot settle both stops for review. |
| `just chunk <slug> <voice=""> <model="">` | Rewrites each paragraph as it should be spoken, then splits it into fragments under the per-language XTTS limit, assigning a cast role to each. The synthesis backend is resolved here, from `config/models.toml`, and the fragments are packed against its limit; pass `model` to override the default for the book's language. Abbreviations and symbols are expanded per language, and `data/book/<slug>/pronunciation.yml` overrides both. Each fragment keeps the printed spelling it came from. |
| `just chunk-single <slug> <voice>` | As above but narrates everything in one voice, ignoring `config/cast.yml`. |

## Stages 4 and 5: make the audio

| Command | What it does |
|---|---|
| `just dryrun <slug> [strict]` | Renders silence at the right durations. No models, no GPU. Pass any value for `strict` to fail on a role whose voice is not cloned. Marks its own output, and `just synth` discards it. |
| `just preview <slug> <voice>` | Renders the first 20 fragments only, to check the voice before committing hours. |
| `just synth <slug> <voice> <device="auto">` | Renders every fragment. Resumable: re-run to continue after an interruption. |
| `just assemble <slug> <format="">` | Muxes fragments, pauses and chapter marks into the finished audiobook. Refuses if the rendered fragments do not match the chunk plan exactly, so an unfinished render cannot become a short audiobook. |

## Dashboard

| Command | What it does |
|---|---|
| `just ui <port="8765">` | Opens the local dashboard at `http://127.0.0.1:8765`. Shows books, voices, renders and quality checks, plays the audio, and runs pipeline stages. |

## Stage 6: quality

| Command | What it does |
|---|---|
| `just resynth <slug> <chunks>` | Re-renders named fragments, comma-separated. Merges into the existing render rather than replacing it. |
| `just verify <slug> <sample="0">` | Re-transcribes the rendered audio and flags chunks that disagree with the source. `sample=20` checks every 20th chunk. |
| `just progress <slug>` | Live state of a running render. Safe from another terminal. |
| `just watch <slug> <interval="5">` | Follows a render until it finishes. |
| `just report <slug>` | Prints the report from the last finished render. |

## Whole runs

| Command | What it does |
|---|---|
| `just book-dry <source> <slug=""> <language="">` | Ingest, chunk, silence, assemble. The whole structure with no model loaded. The audiobook it produces is silent by design; `just synth <slug>` then replaces the silence with narration. |
| `bin/audiobook -v <sample> -b <ebook> [-o <path>]` | Everything, with named inputs and a `~/Downloads` default. |
| `just book <source> <voice> <slug=""> <language=""> <format="">` | Stages 2 to 5 for a voice that is already cloned. |
| `just factory <sample> <voice> <source> <slug> <language="pl"> <format="">` | Everything, positionally. `bin/audiobook` is friendlier. |

## Cleanup

| Command | What it does |
|---|---|
| `just clean-jobs <keep="20">` | Deletes finished dashboard jobs and their logs, keeping the newest. Running jobs are untouched; stale locks are cleared. |
| `just clean-jobs-all` | Deletes every finished job record. |
| `just clean-audio <slug>` | Deletes rendered audio so the next `synth` starts fresh. |
| `just clean-book <slug>` | Deletes the parsed text and its audio. |

## Docker

Two profiles. `cpu` runs anywhere including macOS; `gpu` needs an NVIDIA host
with `nvidia-container-toolkit`, since macOS has no GPU passthrough.

| Command | What it does |
|---|---|
| `just docker-build` | Builds the CPU images: bookbinder and studio. |
| `just docker-build-gpu` | Builds the CUDA images. Never built on Apple Silicon. |
| `just docker-run <service> [args]` | Runs one stage, e.g. `just docker-run bookbinder python -m bookbinder.chunk solaris`. |
| `just docker-smoke [slug] [source]` | Ingest, chunk, silence and assemble entirely in containers. No GPU, no models. |
| `just docker-ui` | The dashboard in a container on `127.0.0.1:8765`. Read-only there: `just` is absent, so the run buttons do nothing. |
| `just docker-down` | Stops everything. |
