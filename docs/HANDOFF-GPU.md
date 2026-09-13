# Handoff: moving to the Windows workstation with the RTX 5090

Written on a MacBook M1 on 2026-09-06, for whoever picks this up on the GPU box.
Revised 2026-09-13 at `v0.2.0`, which added a second synthesis backend and the
benchmark harness that compares it against the first. Everything below was
either measured here or is explicitly flagged as unverified.

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
| Synthesise with Chatterbox | **Adapter written, never run.** No weights on this machine |
| Benchmark two engines | Harness works. **Has never had two engines to compare** |
| **Fine-tune** | **Never run. Needs CUDA. This is the open task.** |
| Dashboard | Works. `just ui`; browse, listen, run stages, correct roles |
| Containers (CPU) | Works. `just docker-smoke` builds and runs with no GPU |
| Containers (CUDA) | **Written, never built.** amd64 only; see below |

1169 tests across five environments (586 bookbinder, 456 studio, 78 narrator,
33 transcriber, 16 chatterbox), pyright and schema checks all pass, and CI is
green on every commit.

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

## The second thing that needs this machine

There are two synthesis engines now, and only one of them has ever made a sound.

`apps/chatterbox/` is a fifth environment holding Chatterbox Multilingual. It
exists because Chatterbox needs transformers 5.2 and torch 2.6 against the
narrator's 4.40 and 2.8, which is the same reason every other environment is
separate. The adapter implements the same `Backend` protocol XTTS does, its
settings are pinned in `config/models.toml`, and its tests pass.

**None of that is evidence it works.** No weights have been downloaded here and
no audio has been generated. The tests prove the adapter has the right shape,
not that the engine produces speech. Treat the first generation as bring-up.

`benchmarks/corpora/pl.toml` and `en.toml` hold eleven passages each across
eight categories: narration, dialogue, numbers, abbreviations, proper names,
short headings, long sentences, chapter transitions. Every eligible model reads
the same text, and results are reported per category and per language. The
harness deliberately refuses to average them into a score, because a model that
reads narration beautifully and mangles numbers is not the same as a mediocre
one.

The first useful command on this box, once CUDA is working, is one run:

```bash
just bench pl michal
```

Both arguments are required: the corpus language and the voice every model
reads it in. `just bench pl michal 3` repeats each passage three times, which
is how stochastic failures show themselves.

That is what B-5 and B-6 in [WORKPLAN.md](WORKPLAN.md) are waiting for. B-6 is
the write-up, `docs/MODEL-EVAL-PL.md` and `docs/MODEL-EVAL-EN.md`, and it needs
someone to listen rather than only to read numbers.

---

## Critical: the CUDA wheels are wrong for this card

**Do not just run `just gpu-torch`.** It points at the CUDA 12.4 index, and those
builds predate Blackwell. The RTX 5090 is compute capability sm_120, and cu124
wheels contain no kernels for it. You will get either a "no kernel image is
available" error or a silent fall back to CPU.

Use the CUDA 12.8 index instead:

```bash
cd apps/narrator    && uv pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu128 --force-reinstall
cd apps/transcriber && uv pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu128 --force-reinstall
cd apps/chatterbox  && uv pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu128 --force-reinstall
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
- chatterbox resolves to **torch 2.6.0**, pulled in by `chatterbox-tts` rather
  than pinned directly. It is the oldest of the three and therefore the most
  likely to have no cu128 build of that exact version. It is also the one the
  benchmark needs, so do not leave it on CPU and call the comparison done.

Get the cu128 build *of those versions* if you can. If a version is unavailable
for cu128, that is a real conflict and worth solving deliberately rather than by
drifting the pin. `just doctor` prints what each environment resolved, and
`just check-narrator` proves XTTS still loads afterwards.

Once torch is swapped, update `justfile`'s `gpu-torch` recipe — it hardcodes
cu124 and defaults to narrator alone — and the `pytorch-cu124` index blocks in
the `pyproject.toml` files, so the next machine does not repeat this.

---

## Windows specifics

The project was built on macOS and CI runs on Linux. It has **never run on
Windows**. As of 2026-09-13 the known obstacles have been removed and the
PowerShell scripts exist, but none of it is proven: you are the first run.

**Native Windows is the intended path, not WSL.** WSL works, but it means
installing ffmpeg and just a second time inside the distro — the scoop ones are
not visible there under their bare names — and it puts GPU passthrough between
you and the card while you are trying to judge whether CUDA is working at all.
If you use WSL anyway, passthrough needs a recent WSL2 with the NVIDIA driver
installed on the Windows side, not inside the distro.

**You still need bash, even natively.** The justfile declares
`set shell := ["bash", "-uc"]`, so every recipe spawns bash. Git for Windows
supplies it. Without it, `just setup` fails immediately with an unhelpful
message about a missing program. This is the one prerequisite people lose an
hour to, so `install.ps1` checks for it by name.

**Setup:**

```powershell
irm get.scoop.sh | iex          # if scoop is not already there
.\install.ps1                   # installs uv, just, ffmpeg, git; then just setup
```

`install.ps1 -Check` reports what is missing and changes nothing. winget works
too and the script uses it when scoop is absent; by hand that is:

```powershell
winget install astral-sh.uv
winget install casey.just
winget install Gyan.FFmpeg
winget install Git.Git        # for bash
```

**PowerShell twins of the bash scripts** landed at the same time, so nothing in
the everyday path requires a bash prompt:

| bash | PowerShell |
|---|---|
| `install.sh` | `install.ps1` |
| `bin/audiobook` | `bin\audiobook.ps1` |
| `scripts/preprocess.sh` | `scripts\preprocess.ps1` |

The options are PowerShell-shaped rather than transliterated: `-Voice`,
`-Book`, `-DryRun` instead of `-v`, `-b`, `--dry-run`. Same stages, same
defaults, same output. If you change one of a pair, change the other.

If PowerShell refuses to run them, that is the execution policy rather than the
script: `powershell -ExecutionPolicy Bypass -File .\install.ps1`.

**What was actually fixed for Windows.** All of it in
`apps/studio/src/studio/process.py`, which is the one module allowed to know
which platform it is on. Four things differed and each mattered:

1. `os.kill(pid, 0)` is a liveness probe on POSIX. On Windows it is not a
   question — it calls `TerminateProcess`. The dashboard would have killed
   every job it looked at. Windows now opens the process and reads its exit
   code.
2. Jobs were launched through `/bin/sh`, which does not exist. The exit-code
   shim is Python on Windows.
3. Stopping a job used the POSIX process group. Windows kills the tree with
   `taskkill /T`, because `just` spawns uv, which spawns python.
4. The venv interpreter is `.venv\Scripts\python.exe`, not `.venv/bin/python`.

Twelve tests cover the seam, including running the Windows exit-code shim on
macOS, since it is ordinary Python. **They do not prove the Windows paths
work** — nothing here can. They prove the branching is right and the logic in
the one testable piece is correct.

**Recording a voice sample.** The runbook's command is macOS-only. On Windows,
ffmpeg uses DirectShow:

```powershell
ffmpeg -list_devices true -f dshow -i dummy          # find the device name
ffmpeg -f dshow -i audio="Microphone (Realtek)" -ar 48000 -ac 1 -t 240 data\raw\voices\you.wav
```

**Playing the audition clip.** `open` is macOS; use `start` in PowerShell.

**Known portability risks**, in rough order of likelihood:

1. Line endings. If git converts to CRLF, the bash scripts fail with an odd
   `\r` error. `git config core.autocrlf input` avoids it. The PowerShell
   scripts do not care.
2. Path separators. The Python code uses `pathlib` throughout, so it should be
   fine, but `data/audio/<slug>/rendered.jsonl` stores paths as strings and they
   are written on the machine that renders. Do not mix machines within one book.
3. The ffmpeg concat list in `bookbinder/assemble.py` writes `as_posix()` paths.
   That is deliberate and should be right for ffmpeg on Windows, but it is
   untested there.
4. The test suite itself is POSIX in places — `test_worker.py` spawns
   `/bin/sh` and uses `killpg`. Tests are expected to run on macOS and Linux;
   if you want `just check` green on Windows, that is unfinished work rather
   than a bug in the pipeline.

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
   ```powershell
   .\bin\audiobook.ps1 -Voice data\raw\voices\michal.wav -Book data\raw\books\test-book.txt -DryRun
   ```
   The bash form, unchanged:
   ```bash
   bin/audiobook -v data/raw/voices/michal.wav -b data/raw/books/test-book.txt --dry-run
   ```
7. Then a real render, and compare the realtime factor in that run's
   `report.json` against the 0.4x measured on the M1. `just catalog-runs <slug>`
   gives the run id and `data/runs/<id>/data/audio/<slug>/report.json` is the
   file. On this card expect it to be far above 1.0. If it is not, CUDA is not
   being used.
8. `just bench pl michal` for the first real comparison between the two
   engines. Expect Chatterbox bring-up here: it has never generated audio, so a
   failure at this step is information about the adapter, not about the card.
9. Only then attempt `just train`.

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

Prefer `just catalog-backup <destination>` over copying the database and assets
by hand. It takes a consistent snapshot of the database together with every
asset it references, checksummed on the way out and again on the way in, and
`just catalog-restore` puts it back. Copying a live database file while Studio
is running is the one way to get a torn one.

**It does not replace the whole list above.** Measured on 2026-09-13: the
backup carries the database, all 55 assets and `config/`, and restore rebuilds
`data/book/`, `data/voices/` and the runs from what the database knows. The
cloned voice is inside it — `latents.pt`, `clone.json` and the audition are all
catalog assets — which matters more than it sounds, because re-cloning on the
other machine would produce a new voice revision and invalidate every fragment
already rendered. What the backup does **not** carry is `data/raw/` and ten of
the twenty-two files in `data/datasets/`. Copy those two directories alongside
it or the fine-tuning input arrives incomplete.

`data/audio/` and `data/out/` are worth leaving behind on purpose: this card
re-renders them faster than a transfer takes.

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
- **Chatterbox has never generated a sample**, so B-5's soaks and B-6's result
  sheets in [WORKPLAN.md](WORKPLAN.md) are both blocked on this machine.
- **Windows has never run any of this.** The obstacles named above were fixed
  blind, from the documented behaviour of the APIs. `just doctor` is the first
  real test.
- **PDF ingestion is untested** on a real book. EPUB and plain text are covered.
- **The narrator's report shape is mirrored by hand** in `synth.py`, because it
  cannot import bookbinder's models. A test pins the two together; if you change
  one, change both.
