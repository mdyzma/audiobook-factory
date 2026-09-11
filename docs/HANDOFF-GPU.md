# Handoff: moving to the Windows workstation with the RTX 5090

Written on a MacBook M1 on 2026-09-06, for whoever picks this up on the GPU box.
Everything below was either measured here or is explicitly flagged as unverified.

Read this first, then [RUNBOOK.md](RUNBOOK.md) for day-to-day use and
[DECISIONS.md](DECISIONS.md) for why the dependency pins exist.

---

## Where the project stands

The pipeline is complete and works end to end. It has produced a real audiobook
in a real cloned voice on Apple Silicon.

| Stage | Status |
|---|---|
| Clean a recording | Works. mp3, wav, m4a, anything ffmpeg reads |
| Label it (WhisperX) | Works. CPU only on Mac; **CUDA on the 5090** |
| Clone the voice | Works. Instant cloning, no training |
| Read an ebook | Works. EPUB, PDF, plain text |
| Chunk with cast roles | Works. Dialogue detection, multi-voice |
| Synthesise | Works. Resumable, 0.4x realtime on M1 |
| Assemble | Works. m4b, mp3 or wav, with chapter marks |
| Verify | Works. Re-transcribes and reports word error rate |
| **Fine-tune** | **Never run. Needs CUDA. This is the open task.** |
| Dashboard | Works. `just ui`; browse, listen, run stages, correct roles |
| Containers (CPU) | Works. `just docker-smoke` builds and runs with no GPU |
| Containers (CUDA) | **Written, never built.** amd64 only; see below |

356 tests, pyright and schema checks all pass, and CI is green on every commit.

## Studio is on the path for every stage now

This changed after the handoff was first written, and it matters here more than
anywhere, because this machine is the one where things get run from a terminal.

`just synth` no longer invokes the narrator. It invokes Studio, which resolves
which run the work belongs to, materialises that run's own data root, and
executes the stage inside it. Same for chunk, dryrun, assemble, verify and
ingest. The isolation is intact and nothing changed about the dependency
split: no pipeline package imports the database, and the stages still read and
write the same manifests. What changed is that `apps/studio` has to be set up
before any of them will run.

Three consequences for this box.

- **`just setup-studio` is not optional.** A broken Studio environment now
  stops every stage, not just the dashboard. `just setup` covers it.
- **Output paths are inside the run, not `data/audio/<slug>/`.** A stage runs
  with its own data root, so a message naming a directory is naming one under
  `data/runs/<run id>/`. `just catalog-runs <slug>` lists them.
- **The library is a database as well as files.** `data/audiobook.db` holds
  which text, model and voice produced which audiobook. Carry it across with
  the data, and see "Data worth carrying across" below.

If you would rather drive the stages directly while debugging CUDA, the
underlying commands still exist: `cd apps/narrator && uv run python -m
narrator.synth <slug>` works against a data root you point
`AUDIOBOOK_FACTORY_ROOT` at. Results produced that way are outside the catalog
until `just catalog-reconcile` picks them up.

---

## The one job that needs this machine

`apps/narrator/src/narrator/train.py` implements XTTS-v2 fine-tuning. Its API surface
was verified field by field against the installed Coqui package, its dataset
formatter is tested, and both guards work. **The training loop itself has never
executed**, because it refuses to start without CUDA and there was none.

Expect to debug it. The Coqui `GPTTrainer` API moved between 0.22 point releases
and the recipe it is modelled on may not match exactly. Treat the first run as
bring-up, not as a regression.

```bash
just train michal          # language pl, 10 epochs, batch 3, accum 84
```

`batch_size * grad_accum` is the effective batch; keep the product near 250.
With 32 GB you should be able to raise `batch_size` to 8 or 16 and drop
`grad_accum` to match. If VRAM runs out, lower `--max-audio-sec` before touching
batch size, since attention cost scales with the square of clip length.

There is a fourth environment now, `apps/studio/`, which is the dashboard. It carries
no ML and does not affect any of the above, but `just setup` builds it too.

There is a working voice dataset already: `data/datasets/michal/`, 18 segments
totalling 1.4 minutes. That is enough to exercise the code path but thin for a
real fine-tune; 30 to 60 minutes of clean audio is the guidance.

---

## Critical: the CUDA wheels are wrong for this card

**Do not just run `just gpu-torch`.** It points at the CUDA 12.4 index, and those
builds predate Blackwell. The RTX 5090 is compute capability sm_120, and cu124
wheels contain no kernels for it. You will get either a "no kernel image is
available" error or a silent fall back to CPU.

Use the CUDA 12.8 index instead:

```bash
cd narrator    && uv pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu128 --force-reinstall
cd apps/transcriber && uv pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu128 --force-reinstall
```

Then confirm the card is actually usable, not merely detected:

```bash
cd apps/narrator && uv run python -c "
import torch
print(torch.__version__, torch.version.cuda)
print(torch.cuda.get_device_name(0), torch.cuda.get_device_capability(0))
print((torch.randn(1000,1000,device='cuda') @ torch.randn(1000,1000,device='cuda')).sum().item())"
```

`get_device_capability` should print `(12, 0)`. If the matrix multiply throws,
the wheels are wrong regardless of what `is_available()` said.

**Mind the version pins while doing this.** Both environments pin torch for
reasons that have nothing to do with CUDA:

- narrator is on **torch 2.8.0**, because torchaudio 2.9 dropped the native
  backends XTTS needs.
- transcriber is on **torch 2.14.0**, because pyannote-audio needs a torchcodec
  build that only recent torch satisfies.

Get the cu128 build *of those versions* if you can. If a version is unavailable
for cu128, that is a real conflict and worth solving deliberately rather than by
drifting the pin. `just doctor` prints what each environment resolved, and
`just check-narrator` proves XTTS still loads afterwards.

Once torch is swapped, update `justfile`'s `gpu-torch` recipe and the
`pytorch-cu124` index blocks in both `pyproject.toml` files, so the next machine
does not repeat this.

---

## Windows specifics

The project was built on macOS and CI runs on Linux. It has **never run on
Windows**. Nothing here is known broken, but none of it is proven either.

**You need bash.** The justfile declares `set shell := ["bash", "-uc"]`, and
`bin/audiobook` and `scripts/preprocess.sh` are bash scripts. Git Bash or WSL
both work; plain PowerShell or cmd will not. If you use WSL, note that GPU
passthrough needs a recent WSL2 with the NVIDIA driver on the Windows side.

**Tooling:**

```powershell
winget install astral-sh.uv
winget install casey.just
winget install Gyan.FFmpeg
```

**Recording a voice sample.** The runbook's command is macOS-only. On Windows,
ffmpeg uses DirectShow:

```bash
ffmpeg -list_devices true -f dshow -i dummy          # find the device name
ffmpeg -f dshow -i audio="Microphone (Realtek)" -ar 48000 -ac 1 -t 240 data/raw/voices/you.wav
```

**Playing the audition clip.** `open` is macOS; use `start` in Git Bash.

**Known portability risks**, in rough order of likelihood:

1. Path separators. The Python code uses `pathlib` throughout, so it should be
   fine, but `data/audio/<slug>/rendered.jsonl` stores paths as strings and they
   are written on the machine that renders. Do not mix machines within one book.
2. The ffmpeg concat list in `bookbinder/assemble.py` writes `as_posix()` paths.
   That is deliberate and should be right for ffmpeg on Windows, but it is
   untested there.
3. Line endings. If git converts to CRLF, the bash scripts will fail with an
   odd `\r` error. `git config core.autocrlf input` avoids it.

---

## What to do first, in order

1. `just setup` then `just doctor`. Confirm numpy is 1.x in narrator and 2.x in
   transcriber. If narrator shows numpy 2.x, stop and read DECISIONS.md. Studio
   is set up by the same command, and every stage now needs it.
2. Swap in cu128 wheels as above, and verify `get_device_capability` is `(12, 0)`.
3. `just check` for schemas, types and tests. Should take seconds.
4. `just catalog-migrate` if you carried an existing `data/` across without its
   database, then `just catalog-check`. It reports the SQLite version Python
   actually loaded, which is worth reading: a runtime predating the WAL-reset
   fix is named there.
5. `just check-narrator` to prove XTTS still loads after the torch swap.
6. Re-run something known good before attempting anything new:
   ```bash
   bin/audiobook -v data/raw/voices/michal.wav -b data/raw/books/test-book.txt --dry-run
   ```
7. Then a real render, and compare the realtime factor in that run's
   `report.json` against the 0.4x measured on the M1. `just catalog-runs <slug>`
   gives the run id and `data/runs/<id>/data/audio/<slug>/report.json` is the
   file. On this card expect it to be far above 1.0. If it is not, CUDA is not
   being used.
8. Only then attempt `just train`.

---

## Numbers measured on the M1, for comparison

| Thing | M1 |
|---|---|
| Synthesis | 0.4x realtime, so ~25 h for a 10 h book |
| Labelling 103 s of audio | a few minutes, CPU only |
| Quality check, 9 fragments | ~1 min with large-v3 on CPU |
| Test suite | 3 s |
| Full `just check` | 8 s |

Everything except the test suite should improve substantially. If synthesis is
not comfortably faster than realtime, something is wrong with the CUDA setup.

## Data worth carrying across

`data/` is gitignored, so clone the repo and copy these by hand if you want them:

- `data/raw/voices/michal.wav` — the original 103 s recording
- `data/datasets/michal/` — 18 labelled segments, the fine-tuning input
- `data/voices/michal.json` and `data/voices/michal/` — the cloned voice
- `data/audiobook.db` and `data/assets/` — the catalog and the bytes it names.
  These two belong together: the database says which text, model and voice made
  a given audiobook, and the assets are what it points at. Copying one without
  the other leaves records naming files that are not there. `just catalog-check`
  will say so.

Prefer `just catalog-backup <destination>` over copying by hand. It takes a
consistent snapshot of the database together with every asset it references,
and `just catalog-restore` puts it back. Copying a live database file while
Studio is running is the one way to get a torn one.

If you carry `data/` across without its database, `just catalog-migrate`
rebuilds the catalog from the files and archives the existing audio as
historical runs. Those runs are honest about what they cannot know: a render
made before any of this existed cannot say which model or voice revision
produced it, and the run records that rather than inventing one.

Model weights are not in the repo. XTTS-v2 downloads on first use, about 1.7 GB.

## Open items besides fine-tuning

- **The CUDA images have never been built.** They now use a `nvidia/cuda:12.8.1`
  base for Blackwell and install from the committed locks, matching the two CPU
  images that are verified. But those bases are amd64 only, so nothing about them
  has been exercised: treat `just docker-build-gpu` as work, not a formality.
  The CPU profile is proven, so the pattern they follow is known good.
  Phases 3 to 5 in ROADMAP-DOCKER.md are the plan from there.
- **PDF ingestion is untested** on a real book. EPUB and plain text are covered.
- **The narrator's report shape is mirrored by hand** in `synth.py`, because it
  cannot import bookbinder's models. A test pins the two together; if you change
  one, change both.
