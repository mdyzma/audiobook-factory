# Bring-up prompt for the RTX workstation

Paste the block below into a fresh Claude Code session on the Windows machine,
in the cloned repository. It deliberately points at the documents rather than
repeating them, so it cannot drift out of date the way a copy would.

---

```
You are picking up audiobook-factory on the Windows workstation with the RTX
5090. Everything in this repository was written and tested on a MacBook M1;
nothing here has ever run on Windows and nothing has ever run on CUDA.

Read these first, in this order, before changing anything:

  docs/HANDOFF-GPU.md      the brief for this machine, and the CUDA problem
  docs/TRANSFER-TO-GPU.md  what was copied across and what was not
  docs/DECISIONS.md        why the environments are split and pinned
  CLAUDE.md                the conventions, including commit style

Your goal, in order. Do not skip ahead: each step is how you find out the
previous one actually worked.

1. Prove the environment. `install.ps1 -Check`, then `just setup`, then
   `just doctor`. Confirm numpy is 1.x in narrator and 2.x in transcriber. If
   narrator shows numpy 2.x, stop and read DECISIONS.md.

2. Expect Windows bugs and fix them. The POSIX assumptions in Studio's job
   runner were removed blind, from documented API behaviour, in
   apps/studio/src/studio/process.py. That module is the only place allowed to
   know which platform it is on; keep it that way. The three PowerShell scripts
   (install.ps1, bin/audiobook.ps1, scripts/preprocess.ps1) have never been
   parsed, let alone run.

3. Get CUDA working. Start with `just gpu-status` and change nothing until you
   have read it. Three environments have torch - narrator, transcriber and
   chatterbox - and two of them already resolve a CUDA build that addresses
   Blackwell, so the first correct action is usually no action. chatterbox is
   the one that cannot: it resolves torch 2.6.0 with CUDA 12.4, and there is no
   cu128 build of 2.6.0 to switch to, so that version has to move and may
   conflict with what chatterbox-tts requires. HANDOFF-GPU.md has the detail.
   A matrix multiply on the device must succeed and get_device_capability must
   report (12, 0); `torch.cuda.is_available()` returning True proves nothing.
   Also check early whether CTranslate2 finds a cuDNN it accepts, because
   WhisperX does not use torch's CUDA and a working torch says nothing about it.

4. Prove the pipeline end to end with a dry run, then a real render of a short
   book. Compare the realtime factor in the run's report.json against the 0.4x
   measured on the M1. If it is not far above 1.0, CUDA is not being used.

5. Run `just bench pl michal`. This is the first time two synthesis engines have
   ever been compared. Chatterbox has never generated a single sample, so treat
   its first generation as bring-up, not as a benchmark result.

Constraints that are load-bearing. Breaking any of these costs more than the
task is worth:

- whisperx and tts==0.22.0 can never share a virtualenv. Never fix a dependency
  error by merging environments or relaxing a pin. If a version has no cu128
  build, that is a real conflict to solve deliberately, not to paper over.
- Do not re-clone the voice. It arrived in the transfer. Re-cloning mints a new
  voice revision, changes every fragment fingerprint, and silently invalidates
  all rendered audio.
- Every command goes through `just`. Add a recipe rather than documenting a raw
  `uv run`, and update docs/COMMANDS.md when you do.
- Tests that need CUDA or model weights do not belong in `just check`.
- `just check` passes on Windows. If it stops passing, that is a regression,
  not a known gap.
- Commit messages: short subject, a body explaining why, never a one-liner and
  never a Co-Authored-By trailer.

How to report. Say what you measured and what you assumed, and keep the two
apart. If a step fails, show the actual output rather than describing it. If
something in the docs turns out to be wrong, fix the document in the same
commit as the code, because those documents are the only handoff this project
has.

Do not start fine-tuning. It is the oldest open task and the most tempting, but
it is worthless until synthesis is proven fast and correct on this card. Ask
before beginning it.

Start by reading the four documents and telling me what you think the first
real obstacle will be.
```

---

## Why it is shaped this way

The prompt names the goal and the order, then gets out of the way. It does not
restate the cu128 commands, the environment table or the transfer list, because
all three already exist in documents the prompt tells it to read, and a second
copy is a second thing to keep true.

The constraints are the ones where a reasonable-looking action does real
damage: merging environments, re-cloning the voice, bypassing `just`. Each is
stated with its consequence, because a rule without a reason is the first thing
an assistant argues itself out of.

The last line asks for a prediction before any work. It is a cheap way to find
out whether the documents were actually read.
