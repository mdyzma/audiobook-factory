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

## Critical: which environments can address this card

Measured on this card on 2026-09-13, after the first `just setup` here.

PyPI's torch carries CUDA on Linux only: its `nvidia-*` runtime packages are
all marked `sys_platform == 'linux'`, and the Windows wheel is CPU-only. The
first `just setup` on this machine therefore gave all three environments
`+cpu` torch. narrator and transcriber now take the same pinned versions from
a PyTorch index on Windows, declared in their `pyproject.toml` under
`[tool.uv.sources]` with a `sys_platform == 'win32'` marker, and locked:

| Environment | torch on Windows | CUDA runtime | Addresses sm_120? |
|---|---|---|---|
| narrator | 2.8.0+cu128 | 12.8 | **yes**, verified: capability (12, 0), matmul, XTTS loads |
| transcriber | 2.14.0+cu130 | 13.0 | **yes**, verified: WhisperX large-v3 labels on CUDA |
| chatterbox | 2.6.0+cpu | — | **no** |
| bookbinder | — | — | no torch at all |
| studio | — | — | no torch at all |

Linux and macOS resolve exactly as before. **So the first thing to do is
nothing:** `just setup`, then `just gpu-status`, which should report the two
builds above. Do not reach for `just gpu-torch` for them; it changes the venv
behind the lock's back, and the next `uv sync` undoes it.

**`just gpu-torch` no longer has a default index**, because it used to install
cu124 into narrator, which would have replaced a working 12.8 build with one
that has no Blackwell kernels. It now requires both the environment and the
index, and `just gpu-status` reports what you actually got.

### chatterbox is the real problem

torch 2.6.0 predates Blackwell support. There is no cu128 build of 2.6.0 to
switch to — CUDA 12.8 wheels start at torch 2.7 — so this cannot be fixed by
pointing at a different index. The version has to move.

It is not pinned directly: `chatterbox-tts` pulls it, and 0.1.7 declares
`torch==2.6.0` and `torchaudio==2.6.0` for every Python below 3.14. That is a
genuine conflict between the second engine and this card, and it is still
open: an override would need evidence that Chatterbox generates correctly on
a newer torch, and a newer release may lift the bound. It is also the environment B-5
needs, so leaving it on CPU and running the benchmark anyway would produce
numbers that mean nothing: one engine on a 5090, the other on a CPU.

### Verifying, whatever you end up with

```bash
cd apps/narrator && uv run python -c "
import torch
print(torch.__version__, torch.version.cuda)
print(torch.cuda.get_device_name(0), torch.cuda.get_device_capability(0))
print((torch.randn(1000,1000,device='cuda') @ torch.randn(1000,1000,device='cuda')).sum().item())"
```

`get_device_capability` must print `(12, 0)`. If the matrix multiply throws,
the wheels are wrong regardless of what `is_available()` said. `is_available()`
returning True is not evidence.

### If you do have to move a pin

Both narrator and transcriber pin torch for reasons that have nothing to do
with CUDA, and those reasons still hold:

- narrator is on **torch 2.8.0**, because torchaudio 2.9 dropped the native
  backends XTTS needs.
- transcriber is on **torch 2.14.0**, because pyannote-audio needs a torchcodec
  build that only recent torch satisfies.

`just doctor` prints what each environment resolved, and `just check-narrator`
proves XTTS still loads afterwards.

### A second CUDA stack, in transcriber only

WhisperX runs its ASR through CTranslate2 (4.8.2 here), which does not use
torch's CUDA at all — it loads cuBLAS and cuDNN itself. A working torch is
therefore not evidence that transcription will run on the GPU.

Measured here: CTranslate2's Windows wheel is built against CUDA 12 and failed
at the first encode with `Library cublas64_12.dll is not found`, while torch
reported everything fine. torch 2.14.0 has no cu128 build to align with (that
index stops at 2.11), so transcriber carries `nvidia-cublas-cu12` on Windows
and `transcriber/cuda_libs.py` loads it into the process before `import
whisperx`. It has to be loaded, not just put on PATH: CTranslate2 asks for the
DLL by bare name. cuDNN needed nothing, because CTranslate2 uses the
`cudnn64_9.dll` torch has already loaded. Labelling the 1.7-minute sample with
large-v3, VAD and Polish alignment took 30 s on the card.

`onnxruntime` in the same environment is the CPU build, used for voice activity
detection. That is cheap and deliberate; it does not need a GPU.

---

## Windows specifics

The project was built on macOS and CI runs on Linux. It first ran on this
Windows workstation on 2026-09-13. `just check` passes here from PowerShell,
and XTTS loading and WhisperX labelling have run on the card. A full render
and the dashboard driving real jobs have not yet.

**Native Windows is the intended path, not WSL.** WSL works, but it means
installing ffmpeg and just a second time inside the distro — the scoop ones are
not visible there under their bare names — and it puts GPU passthrough between
you and the card while you are trying to judge whether CUDA is working at all.
If you use WSL anyway, passthrough needs a recent WSL2 with the NVIDIA driver
installed on the Windows side, not inside the distro.

**You still need bash, even natively.** Every recipe runs under bash, and
Git for Windows supplies it. On Windows the justfile names it by path,
`set windows-shell := ["C:/Program Files/Git/bin/bash.exe", "-uc"]`, because
plain `bash` resolves to `C:\Windows\System32\bash.exe` first: the WSL
launcher, which fails with `execvpe(/bin/bash) failed` when no Linux distro is
installed. That made every recipe fail from PowerShell while working from a
Git Bash prompt. `install.ps1` checks for the file at that path, since `bash`
on PATH proves nothing, and installs Git with winget even under scoop, which
would put it somewhere else.

**Python runs in UTF-8 mode.** The justfile exports `PYTHONUTF8=1`. Without it
Windows Python writes piped output and opens files in cp1250 on a Polish
machine, so job logs and anything read back as UTF-8 show `�` for Polish
letters. Three bookbinder and studio tests failed on exactly that.

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

**What was actually fixed for Windows.** Everything about processes and locks
is in `apps/studio/src/studio/process.py`, which is the one module allowed to
know which platform it is on. Five things differed and each mattered:

1. `os.kill(pid, 0)` is a liveness probe on POSIX. On Windows it is not a
   question — it calls `TerminateProcess`. The dashboard would have killed
   every job it looked at. Windows now opens the process and reads its exit
   code.
2. Jobs were launched through `/bin/sh`, which does not exist. The exit-code
   shim is Python on Windows.
3. Stopping a job used the POSIX process group. Windows kills the tree with
   `taskkill /T`, because `just` spawns uv, which spawns python.
4. The venv interpreter is `.venv\Scripts\python.exe`, not `.venv/bin/python`.
5. Catalog imports and run stages locked with `fcntl.flock`, and `fcntl` does
   not exist on Windows, so importing it failed and took both down. Found only
   by running the suite here; the blind fixes above had missed it.
   `process.exclusive` locks with `msvcrt.locking` on Windows instead.

Three more surfaced the same way, outside that module. Stored relative paths
(`source_file`, `audio_path`, a voice's `model_dir`, benchmark results) were
written with `str()`, so a Windows machine wrote backslashes into files a Mac
or Linux machine reads; they are `as_posix()` now. Publishing the migrated
catalog fsynced a file opened read-only, which Windows refuses. And `bash`
resolved to the WSL launcher, covered above.

The tests now run here: `test_worker.py` spawns its stand-in child with the
test interpreter rather than `/bin/sh`, the zombie-reaping test is skipped as
POSIX-only, and the three symlink containment tests skip unless Developer Mode
allows creating file symlinks (a directory symlink falls back to a junction).

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
2. Path separators. Stored relative paths are written with forward slashes on
   every platform now, but books rendered before that fix on Windows would
   carry backslashes. None exist yet; this machine had not rendered one.
3. The ffmpeg concat list in `bookbinder/assemble.py` writes `as_posix()` paths.
   The assembly tests pass here against real ffmpeg, so that holds on Windows.

---

## What to do first, in order

1. `just setup` then `just doctor`. Confirm numpy is 1.x in narrator and 2.x in
   transcriber. If narrator shows numpy 2.x, stop and read DECISIONS.md. Studio
   is set up by the same command, and every stage now needs it.
2. `just gpu-status`. narrator and transcriber should report capability
   `(12, 0)` as locked; see above before changing any torch.
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
- **Windows has run the checks but not the pipeline.** `just check` passes and
  both CUDA environments work on the card, but no book has been rendered here
  and the dashboard has not driven a real job on Windows.
- **PDF ingestion is untested** on a real book. EPUB and plain text are covered.
- **The narrator's report shape is mirrored by hand** in `synth.py`, because it
  cannot import bookbinder's models. A test pins the two together; if you change
  one, change both.
