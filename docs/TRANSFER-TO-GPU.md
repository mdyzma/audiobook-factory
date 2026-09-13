# What to send to the RTX workstation

Inventoried on the MacBook on 2026-09-13 at commit `0acb390`. Sizes are real,
measured, not estimates. Assumes a fast local link, so this errs towards
sending things that could be re-downloaded.

The repository itself is **not** in this list: clone it from GitHub, because a
copied working tree brings a `.git` with macOS paths in its config and every
`.venv` with it.

---

## 1. Clone first, then send into the clone

```bash
git clone git@github.com:mdyzma/audiobook-factory.git
cd audiobook-factory
git config core.autocrlf input
```

`core.autocrlf` before anything else. CRLF line endings break the bash scripts
with an error that never mentions line endings, and `just` runs every recipe
through bash.

---

## 2. Send: project data — 90 MB

| Path | Size | Why |
|---|---|---|
| `data/assets/` | 17 MB | Content-addressed store. The bytes the catalog names. |
| `data/audiobook.db` | 8.9 MB | The catalog. Which text, model and voice made which audiobook. |
| `data/voices/` | 392 KB | **The cloned voice.** `latents.pt`, `clone.json`, the audition. |
| `data/raw/` | 10 MB | Original recordings, including the 103 s `michal.wav`. |
| `data/datasets/` | 3.9 MB | The 18 labelled segments. Fine-tuning input. |
| `data/book/` | 2.6 MB | Per-book text, chunk plans, `pronunciation.yml`. |
| `data/processed/` | 4.7 MB | Cleaned recordings. Derivable, but cheap. |
| `data/audio/` | 15 MB | Rendered fragments. Carry them and resume works. |
| `data/out/` | 356 KB | Finished audiobooks. |
| `data/runs/` | 2.3 MB | Per-run data roots. |

**Do not re-clone the voice on the other machine.** Re-cloning mints a new
voice revision, which changes every fragment fingerprint and invalidates all
rendered audio. Carrying `data/voices/` is what keeps resume honest.

Skip `data/backups/` (25 MB) — snapshots of what you are already sending.

Simplest is to send all of `data/` and delete `data/backups/` after. The
alternative, `just catalog-backup`, is the right tool over a slow link but only
covers the database, its assets and `config/`; see HANDOFF-GPU.md.

Send with nothing running, or the database can be torn mid-copy.

---

## 3. Send: model weights — 6.1 GB

Platform-independent files. Sending them saves a long first-run download on a
machine you want to be testing CUDA on, not waiting for HuggingFace.

| From (macOS) | To (Windows) | Size |
|---|---|---|
| `~/Library/Application Support/tts/` | `%LOCALAPPDATA%\tts\` | 1.7 GB |
| `~/.cache/huggingface/hub/models--Systran--faster-whisper-large-v3/` | `%USERPROFILE%\.cache\huggingface\hub\` | 2.9 GB |
| `~/.cache/huggingface/hub/models--jonatasgrosman--wav2vec2-large-xlsr-53-polish/` | same | 2.4 GB |
| `~/.cache/huggingface/hub/models--Systran--faster-whisper-tiny/` | same | 75 MB |

The first is XTTS-v2, including `tos_agreed.txt`, so the licence prompt does
not reappear. The second is WhisperX's ASR model and the third is its Polish
alignment model. `~/.local/share/tts/` holds the same XTTS files again under
the older path; send either one, not both.

**Do not send** `~/.cache/huggingface/token` or `stored_tokens` — those are
your HuggingFace credentials, and nothing here needs them.

**Do not send** the other models in that cache. `models--google--gemma-2-2b`
(9.8 GB), `Kokoro-82M` and `all-MiniLM-L6-v2` belong to other projects.

The English alignment model is absent here (`~/.cache/torch` is empty), because
nothing English has been transcribed on this machine yet. It downloads on first
use.

---

## 4. Do not send

| Path | Size | Why not |
|---|---|---|
| `apps/*/.venv/` | 4.4 GB | macOS arm64 binaries. Rebuild with `just setup`. |
| `~/.cache/uv/` | 6.4 GB | Cached macOS wheels. Useless to Windows, and uv refills it. |
| `.git/` | 2.7 MB | Comes with the clone. |
| `pytest-of-michaldyzma/` | 2.8 MB | Leftover pytest scratch. |
| `uv-*.lock` (5 files, 0 B) | — | Stray resolver scratch. |
| `.DS_Store` | 8 KB | — |
| `gemini-session.md` | 144 KB | Deliberately untracked; its conclusions are in DECISIONS.md. |

The five 0-byte `uv-*.lock` files and `pytest-of-*` are already gitignored, so
they are noise rather than a problem.

---

## 5. On the other side

```powershell
.\install.ps1              # uv, just, ffmpeg, Git for Windows, then just setup
just doctor                # numpy 1.x in narrator, 2.x in transcriber
just catalog-check         # integrity, and the SQLite version actually loaded
just check                 # will not pass on Windows yet; see HANDOFF-GPU.md
```

`just catalog-check` is the one that proves the transfer worked: it verifies
every asset the database names is present and matches its checksum.

Then read HANDOFF-GPU.md before `just gpu-torch`. The CUDA 12.4 wheels it
installs have no kernels for Blackwell, which is still the unsolved problem
standing between a working clone and a working render.
