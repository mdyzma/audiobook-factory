# Runbook

Everyday tasks, in the order you actually hit them. Every block below is real
output from a real run on an M1, not an illustration.

There are no screenshots: this is a command-line tool, so the terminal
transcripts *are* the screenshots.

For the full list of recipes see [COMMANDS.md](COMMANDS.md). For why anything is
pinned, [DECISIONS.md](DECISIONS.md).

---

## Before anything else: is the machine healthy?

```
$ just doctor
== ffmpeg ==
ffmpeg version 9.0.1 Copyright (c) 2000-2026 the FFmpeg developers
== uv ==
uv 0.12.7 (Homebrew 2026-08-27 aarch64-apple-darwin)
== transcriber ==
numpy 2.4.6 | pandas 3.0.5 | torch 2.14.0 | cuda False
== bookbinder ==
ok
== narrator ==
numpy 1.26.4 | torch 2.8.0 | transformers 4.40.2 | cuda False
```

What to look for:

- **narrator numpy must start with 1.** If it says 2.x, XTTS will fail at
  inference. Something you installed lifted it.
- **The two torch versions differ on purpose.** 2.14 in the transcriber for
  torchcodec, 2.8 in the narrator because torchaudio 2.9 dropped the backends
  XTTS needs. Separate virtualenvs is what makes that legal.
- **`cuda False` on a Mac is correct.** MPS is used where it can be.

---

## The short way

If you just want an audiobook and do not care about the stages:

```
$ bin/audiobook --voice /tmp/michal.mp3 --book /tmp/solaris.epub
voice sample : /tmp/michal.mp3  (as 'michal')
ebook        : /tmp/solaris.epub  (as 'solaris')
output       : /Users/michaldyzma/Downloads/solaris.m4b
language     : pl

==> Voice 'michal' already cloned; reusing it (--reclone to redo)
==> Reading the ebook
Solaris Testowa - Stanisław Lem
2 chapters, 23 words -> data/book/solaris/chapters.json
==> Splitting it into fragments
5 chunks across 2 chapters (limit 224 chars for 'pl', 0 oversize)
==> Synthesising (resumable: re-run this command to continue)
rendered 5/5 chunks (0 already present), 0.00 h of audio in 0.8 min (0.3x realtime)
==> Assembling
2 chapters, 00:00:16.161, 0.1 MB

Done. /Users/michaldyzma/Downloads/solaris-demo.m4b
```

It derives the voice name and the book name from the filenames, clones the voice
only if it has not been cloned before, and copies the result to `~/Downloads`.

- `-o ~/Music/solaris.mp3` picks both the location and the format. `.m4b`,
  `.mp3` and `.wav` are supported; a directory gets an m4b.
- `--dry-run` renders silence instead of speech, so the structure is checkable
  in seconds.
- `--verify` re-transcribes the result afterwards.
- Interrupting is safe. Re-run the same command and it continues.
- It repoints `config/cast.yml` at your voice only when the cast names a single
  voice. A hand-built multi-voice cast is left alone, with a note if the voice
  you passed is not in it.

The rest of this runbook is the same work done stage by stage, which is what you
want when something needs attention.

## Task: add a new voice

Roughly ten minutes, most of it reading aloud.

**1. Record two to three minutes.** Read [the reading
script](reading-script.txt); it covers Polish phonetics,
questions, numbers and dialogue, so the clone learns your reading voice rather
than one register.

```bash
ffmpeg -f avfoundation -i ":0" -ar 48000 -ac 1 -t 240 data/raw/voices/michal.wav
```

Press `q` when you finish. Mistakes cost nothing: the recording gets cut into
fragments, so a repeated sentence just becomes another fragment.

**2. Clean, label, clone.** One command for all three:

```
$ just voice data/raw/voices/michal.wav michal
cleaned -> data/processed/michal/cleaned_full.wav (1.7 min, 24kHz mono s16)
18 segments (1.4 min) -> data/datasets/michal
voice profile -> data/voices/michal.json
cloning 'michal' from 12 references on mps
latents cached -> data/voices/michal/latents.pt
audition -> data/voices/michal/audition.wav
```

Labelling is the slow part. WhisperX runs on CPU here, because its backend has
no Metal support, so budget a few minutes and longer on the first run while the
`large-v3` model downloads.

**3. Listen before trusting it.**

```bash
open data/voices/michal/audition.wav
```

If it sounds thin or metallic, the sample is usually the problem, not the model.
Re-record somewhere quieter, or read for longer. Roughly 1.4 minutes of usable
audio is the floor for instant cloning.

**4. Point the cast at it.** Edit `config/cast.yml`:

```yaml
roles:
  narrator:
    voice: michal
```

---

## Task: turn an ebook into an audiobook

**1. Check the structure first.** This costs seconds and loads no model:

```
$ just book-dry data/raw/books/lem.epub solaris
2 chapters, 86 words -> data/book/solaris/chapters.json
9 chunks across 2 chapters (limit 224 chars for 'pl', 0 oversize)
2 dialogue chunks; cast: dialogue->michal, narrator->michal
dry run: 9/9 chunks, 0.7 min of silence in 0.3 s
2 chapters, 00:00:43.650, 0.0 MB
-> data/out/solaris.m4b
```

Read the numbers, not just the exit code:

- **`0 oversize`** matters most. XTTS truncates past the per-language limit
  without raising anything, so an oversize chunk silently loses text.
- **Chapter count** should match the book. If it is 1, the parser found no
  headings and the whole book will be one chapter.
- **The cast line** shows which voice each role resolved to. A role you expected
  to be a character but which reads `->michal` means the speaker label did not
  match a cast entry.

Then delete the silent m4b and do it properly.

**2. Sample the voice on this book** before committing hours:

```bash
just preview solaris michal    # renders 20 fragments only
```

**3. Render.**

```
$ just synth solaris michal
synthesising 9 chunks on mps
voices: michal
rendered 9/9 chunks (0 already present), 0.01 h of audio in 1.9 min (0.4x realtime)
```

**0.4x realtime is the number that matters.** A ten-hour audiobook is about 25
hours of work on an M1. Start it before you go to bed, or move to the CUDA box.

Interrupting is safe. Fragments that already have audio are skipped, so
re-running continues:

```
rendered 9/9 chunks (9 already present), 0.01 h of audio in 0.0 min (3091.1x realtime)
```

**Watching a long render.** `report.json` only appears at the end, so use the
progress file instead. From any other terminal:

```
$ just progress solaris
solaris  [###...........................] 11.1%   running
  1/9 fragments  (1 rendered, 0 already present)
  34s elapsed, about 4m 36s left  |  2s of audio on mps
  at ch001_0000 in 'michal'
```

`just watch solaris` refreshes it until the render finishes.

Two things to know about the estimate. It is based on the rate so far, so the
first reading is pessimistic: model loading takes about thirty seconds and is
charged to the first fragment. And if the state says `STALE`, the process wrote
nothing for two minutes and has probably died, whatever `running` claims.

**4. Assemble.**

```
$ just assemble solaris
test-book - Unknown
2 chapters, 00:00:43.855, 0.3 MB
-> data/out/solaris.m4b
```

---

## Task: check a finished audiobook

Synthesis fails quietly. XTTS can truncate a fragment, skip a clause or repeat a
phrase, and none of that raises anything.

```
$ just verify solaris
checking 9 chunks with whisperx large-v3 on cpu
mean WER 0.054 over 9 chunks; 1 above 0.15
-> data/audio/solaris/qa_report.json
  ch002_0005 wer=0.33
    said:  Rozdział drugi: Lustra
    heard: Rozdział 2. Lustra.
```

**Expect a non-zero baseline.** The transcriber mishears too. The example above
is not a defect: the clone read the ordinal correctly and Whisper wrote it back
as a digit. Look for outliers, especially fragments where the tail is missing,
which is what a truncation looks like.

On a real book, sample rather than checking everything:

```bash
just verify solaris 20     # every 20th fragment
```

---

## Task: read the report from a run

Every render writes one, real or dry.

```
$ just report solaris
{
    "slug": "solaris",
    "cast": { "dialogue": "michal", "narrator": "michal" },
    "device": "mps",
    "dry_run": false,
    "elapsed_sec": 111.542,
    "chunks_total": 9,
    "chunks_rendered": 9,
    "chunks_skipped": 0,
    "audio_sec": 39.605,
    "failures": [],
    "realtime_factor": 0.36,
    "ok": true
}
```

`ok` is false when anything failed or went unrendered. `failures` names the
fragment and the error. A single bad fragment never kills a run, so this is the
only place a partial failure shows up.

---

## Task: cast dialogue to a second voice

Clone the second voice as above, then add it:

```yaml
roles:
  narrator:
    voice: michal
  kelvin:
    voice: kelvin
```

The key must match the speaker label in the text, lowercased with spaces
hyphenated, so `Kelvin: — Wracam na Ziemię.` maps to `kelvin`.

Re-chunk so the roles are reassigned, then check the cast line:

```
$ just chunk solaris
2 dialogue chunks; cast: dialogue->michal, kelvin->kelvin, narrator->michal
```

`just dryrun solaris strict` fails if any role maps to a voice you have not
cloned, which is worth running once the cast is meant to be complete.

---

## When something looks wrong

**Chapter count is 1 when the book has many.** The parser found no headings.
EPUBs usually work; plain text needs markdown-style `#` headings.

**A character reads in the narrator's voice.** Either the speaker label is not
in `config/cast.yml`, or the label is not the shape the detector recognises.
Detection is typographic and deliberately cautious: it would rather leave
dialogue with the narrator than put narration in a character's voice.

**`n oversize` is not zero.** Chunks exceed the model limit and will be
truncated silently. Report it; the packer should make this impossible.

**Synthesis is far slower than 0.4x realtime.** Check `device` in the report. If
it says `cpu` on a Mac, MPS was not picked up.

**numpy 2.x in the narrator.** Something you installed lifted it. Constrain the
culprit in `narrator/pyproject.toml` under `[tool.uv] constraint-dependencies`,
then `just relock narrator`.

More failure modes, with causes, in [DEVELOPMENT.md](DEVELOPMENT.md).

---

## Moving to the CUDA machine

The stages are independent, so labelling can happen on the laptop and synthesis
on the GPU box. Copy `data/` across; nothing else is needed.

```bash
just setup
just gpu-torch narrator
just gpu-torch transcriber
```

Fine-tuning needs CUDA outright and will refuse to start without it. Instant
cloning does not, and covers most books.
