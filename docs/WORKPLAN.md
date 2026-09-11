# Workplan

Derived from [ROADMAP-ASSESSMENT.md](ROADMAP-ASSESSMENT.md), which reprioritised the
59 proposals in [ROADMAP-IMPROVEMENTS.md](ROADMAP-IMPROVEMENTS.md) against the stated
product goal. Written 2026-09-09 against code baseline `5a35a01`, which is
byte-identical to the assessment baseline `a265f90` outside `docs/`.

This document does not re-argue priorities. It converts the assessment's six
workstreams and five delivery slices into ordered work items with named files,
new modules, `just` recipes, tests, and exit evidence.

## Current state, verified today

Read before planning any item: each claim below was checked against the code at
`5a35a01`, not carried over from the assessment. **Every row was fixed in slice
0 below**, which is recorded here rather than deleted so the next slice can see
what the code used to do.

| Site | Condition |
|---|---|
| [ingest.py:150](../apps/bookbinder/src/bookbinder/ingest.py#L150) | `read_text(encoding="utf-8", errors="replace")`. Legacy-encoded Polish loses characters before anything else runs. |
| [ingest.py:96,137,151](../apps/bookbinder/src/bookbinder/ingest.py#L96) | Language defaults to `pl` for TXT; EPUB trusts `DC:language` metadata with a `pl` fallback. No detection anywhere. |
| [chunk.py:68](../apps/bookbinder/src/bookbinder/chunk.py#L68) | `current = word.strip()[:limit]` truncates an overlong token. Text is discarded silently. |
| [chunk.py:152](../apps/bookbinder/src/bookbinder/chunk.py#L152) | Chunk limit comes from a global XTTS table keyed by language, before any model is resolved. |
| [synth.py:242](../apps/narrator/src/narrator/synth.py#L242) | An existing WAV is reused on nothing but its filename. No fingerprint, no model identity. |
| [synth.py:93](../apps/narrator/src/narrator/synth.py#L93) | `VoicePool` holds one `_model`. The first voice's checkpoint serves every later voice, including fine-tuned ones. |
| [engine.py:14,61](../apps/narrator/src/narrator/engine.py#L14) | `DEFAULT_MODEL` and 24 kHz are compiled into the engine. There is no backend contract. |
| [assemble.py:153](../apps/bookbinder/src/bookbinder/assemble.py#L153) | A missing fragment prints to stderr and is skipped. The book still assembles. |
| [data.py:116](../apps/studio/src/studio/data.py#L116) | `if self.outputs: return "done"`. Completion is inferred from an output file existing. |
| [jobs.py:252](../apps/studio/src/studio/jobs.py#L252) | `acquire` checks the holder, then writes the lock. Two callers can pass the check. |
| [authoring.py:82,93,100](../apps/studio/src/studio/authoring.py#L82) | The upload writes straight to its final path and unlinks on failure, destroying a pre-existing file of the same name. |
| [verify.py:122](../apps/transcriber/src/transcriber/verify.py#L122) | `chunks[0]["language"]` picks the ASR model for the whole run. |
| [paths.py](../apps/bookbinder/src/bookbinder/paths.py) | Roots resolve, but serving and staging containment still need canonical, symlink-aware checks. |

What already exists and should be built on, not replaced: five exported JSON
schemas, Pyright plus 396 tests across four environments, per-environment locks
and pins, detached jobs with persisted PIDs, upload extension and size limits,
`check_name` filename validation, atomic cast writes via temp-and-rename, and
the `just` recipe surface.

## Working agreements

These follow [CLAUDE.md](../CLAUDE.md) and apply to every item below.

- `whisperx` and `tts==0.22.0` never share a virtualenv. A new synthesis backend
  whose pins conflict gets its own environment under `apps/`, not a relaxed
  constraint in `apps/narrator/`.
- Environments exchange data only through files under `data/`. A "backend
  contract" therefore means a versioned JSON schema plus per-environment models
  that validate it, never a shared imported Python class.
- Every command gets a `just` recipe. Update [COMMANDS.md](COMMANDS.md) in the
  same change.
- A manifest field change starts in
  [manifest.py](../apps/bookbinder/src/bookbinder/manifest.py), bumps
  `SCHEMA_VERSION`, regenerates `docs/schemas/`, and updates both consumers.
- Tests live in `<env>/tests/` and run against that environment's own
  dependencies. Nothing needing model weights or CUDA enters the default suite.
- `just check` passes before every commit.
- Terminal output in [RUNBOOK.md](RUNBOOK.md) is captured from real runs. If an
  item changes what a command prints, re-capture the block.

Sizes are relative: **S** is a focused change with its tests, **M** is a new
module or a contract change across environments, **L** needs hardware time or
spans all four environments. No calendar estimates, per the assessment.

## Slice 0 — Defect sweep — done

Independent of the architecture work, cheap, and each one currently destroys or
misreports output. Doing these first means later slices are built on a pipeline
whose failures are visible. Every item lands with a regression test (TEST-05).

Completed 2026-09-09. What each one turned out to be:

| ID | Work | What was actually wrong |
|---|---|---|
| D-01 | EPUB spine ordering, nested-block duplication, omitted text-only containers | All three, and worse than described. Chapters came back in manifest order, a `blockquote` wrapping a `p` was narrated twice, and a chapter built from bare `div`s was dropped entirely with no message. |
| D-02 | TXT preamble, de-hyphenation ordering | Everything before the first markdown heading was discarded. De-hyphenation could never fire, because `_paragraphs` collapsed the newlines its pattern matched, so a wrapped word came out as "prze- rwa". |
| D-03 | Overlong-token truncation in `hard_split` | A 500-character token kept 224 characters and dropped 276. The existing test only checked the length of each piece, never the content. |
| D-04 | Missing fragments in assembly | Both a deleted wav and a partly failed render assembled into a short book. Assembly never compared the rendered fragments against the chunk plan at all. |
| D-05 | `done` derived from output existence | A failed render left the previous export in place and the book reported itself finished. The report's verdict now outranks the file. |
| D-06 | Path containment | The name checks already blocked traversal. The gap was symlinks, which resolve past a legitimate name. |
| D-07 | Shell and ffmpeg escaping | `--title '<value>'` was built by concatenation, so an apostrophe in a title injected shell. An apostrophe in a path broke the ffmpeg concat list, and a newline in an ebook title wrote extra tags into the finished audiobook. |
| D-08 | Destructive failed re-upload | The upload opened the target directly, truncating it, then deleted it on failure. Re-uploading a book you already had destroyed the copy you had. |
| D-09 | Lock races and overlapping stages | `acquire` was check-then-write. Separately, any non-locking stage could run on a book mid-render. |
| D-10 | One checkpoint across voices | `VoicePool` cached a single model, so a fine-tuned narrator's weights narrated every other voice in the cast. |

**Exit evidence.** `just check` green: five schemas, Pyright clean in all four
environments, and 454 tests, up from 396 at `5a35a01`. Each defect has a test
that fails on the previous code and passes now; D-01, D-02, D-03 and D-10 were
additionally confirmed by running the new tests against the committed version.
Assembly refuses a book with a deleted fragment, an unrendered fragment, a
duplicate, or ids outside the chunk plan.

## Slice A — Text and language (TEXT-01, LANG-01) — done

The assessment's workstreams 1 and 2. Nothing downstream can recover characters
or passages lost here.

Completed 2026-09-09.

| ID | Work | Size | Outcome |
|---|---|---|---|
| A-1 | Immutable source import. Copy each input under `data/sources/<book_id>/` with its SHA-256. Later edits to the input folder cannot change a queued book. | M | Staged copy written by rename, hashed from the copy. `source_file` points at it and `original_source` records where it came from. Editing or deleting the original afterwards changes nothing. |
| A-2 | New `bookbinder/decode.py`. Explicit override, then BOM, then strict UTF-8, then the candidate set. Rank ambiguity with `charset-normalizer` plus text-quality checks. No `replace`, no `ignore`. Store accepted text as NFC UTF-8. | L | Done, with the detector demoted to a tie-breaker; see the finding below. |
| A-3 | EPUB decodes through its parser, honouring declared XHTML encodings per document. No whole-file decoder over ZIP bytes. Flag malformed declarations. | M | Done. Documents whose decoded text reads as neither language are named, and `--encoding` is reported as inapplicable rather than ignored. |
| A-4 | Keep three representations with anchors between them: source bytes, extracted text, spoken text. Record every transformation. | M | Staged bytes, `Chunk.source_text`, `Chunk.text`. Every substitution carries its span on both sides, and `source_text` is stored only where it differs. |
| A-5 | New `bookbinder/language.py`. Sample across the book, excluding navigation and boilerplate. Cross-check EPUB metadata against content. Abstain to `needs_review` rather than defaulting to `pl`. | L | Done. Short, ambiguous, mixed and third-language books stop instead of becoming Polish. |
| A-6 | Manifest: the encoding and language provenance, `needs_review`, `review_reasons`. Bump schema, regenerate, update consumers. | M | Schema 3. Two bumps: 2 for the book-level provenance, 3 for the per-chunk spelling and substitutions. The exporter now deletes superseded files. |
| A-7 | Language-aware spoken-text preparation and a per-book pronunciation dictionary. Conservative and previewable. Applied before token-budget validation. (FEAT-01 basic) | L | Abbreviations and symbols per language, plus `pronunciation.yml` per book. Preparation runs before packing, so the character budget measures what the model reads. Numbers are deliberately left alone; see below. |
| A-8 | Fixture corpus (TEST-08, TEST-03). | M | Done: four encodings of one Polish text, Polish and English abbreviations, possessives and contractions, an out-of-order EPUB spine, English front matter in a Polish book, wrong EPUB metadata, a short ambiguous file, and German. |
| A-9 | Surface it. `just ingest` gains `--encoding`, `--language`, and a review flag. Studio shows the encoding and detected language. (DX-02, DX-01 subset) | M | Both halves. `just inspect` reports the evidence without writing; the dashboard shows encoding, language and review state, and the book page shows the samples and warnings behind them. |

**Numbers are left as digits, on purpose.** Polish inflects numerals for case
and gender, so "3 koty" and "o 3 kotach" need different words and a rule that
cannot tell them apart makes narration worse rather than better. The machinery
for expansion exists in the per-book dictionary, where a person decides.
Whether the models read digits acceptably is a listening question, and belongs
to slice B.

**Exit evidence.** `just check` green: schema 3, Pyright clean in all four
environments, 601 tests, up from 454 at the start of slice 0. The same Polish
text in UTF-8, UTF-16, Windows-1250 and ISO-8859-2 decodes to identical
accepted text. A mixed folder routes each book by its own words. A Windows-1250
Polish book was imported, chunked and shown in the dashboard end to end, with
every substitution span verified against both representations.

**Decision, settled 2026-09-09: `lingua-language-detector`.** All four
candidates install on Python 3.11.9, so the binding was not the discriminator.
Two things were: its confidence is calibrated, scoring real prose 0.93 to 1.00
and four-word fragments 0.06 to 0.12, which is what the abstention rule needs
and what `langdetect` fails at, being 0.86 confident and wrong on "Tak."; and
its models ship inside the wheel, so unlike `fastText lid.176` there is no
126 MB file to download and pin separately, which suits an offline tool better.
It costs 97 MB installed, 82 MB resident and 0.24 s for the first call.

**Finding: a charset detector cannot be trusted with this decision.** On real
ISO-8859-2 Polish, `charset-normalizer` returns `iso8859_10`, which decodes
without error and silently changes the diacritics. Every candidate is therefore
decoded strictly and its text scored, with the detector used only to order
otherwise-equal readings. The scoring has to look at more than letters:
Windows-1250 and ISO-8859-2 differ for Polish mainly in characters that
mis-decode into punctuation, `ą` into `±` and `ś` into `¶`, so a letters-only
measure scored both readings perfect and picked whichever came first.

**Exit evidence.** The same Polish text in UTF-8, UTF-16, Windows-1250, and
ISO-8859-2 yields identical accepted Unicode. Ambiguous and corrupt inputs pause
without text loss. A mixed folder routes each book correctly or abstains
explicitly. Re-import preserves every intended passage once, in order.

## Slice B — Model selection (MODEL-01)

Begins during slice A, using A's accepted text. The assessment is explicit that
this must not wait for XTTS to fail.

| ID | Work | Size |
|---|---|---|
| B-1 | Versioned backend request and result schemas in `docs/schemas/`, exported from `manifest.py` like the existing five. Backend-neutral: text, language, voice reference, effective settings, and the resulting audio plus its native rate and engine identity. (ARCH-05 subset) | M — **done** |
| B-2 | Model registry in `config/models.toml`. Per entry: narration and reference languages, reference-audio and transcript requirements, cloning support, token and context limits, native rate, supported controls, runtime environment, exact checkpoint and tokenizer revision, asset hashes, validation status. Resolution order is explicit book or role override, then the validated default for the book language. Never substitute silently. (SEC-02, CONF-01) | M — **done** |
| B-3 | Refactor the narrator behind the contract. `narrator/backends/xtts.py` implements it; `engine.py` stops being the only path. The pinned environment is untouched. | M — **done** |
| B-4 | First alternative backend in its own environment. Recommended: Chatterbox Multilingual, because it documents both `pl` and `en` and so serves the Polish comparison and the English one from a single new environment. (ARCH-07, FEAT-18 adapters) | L |
| B-5 | Benchmark harness and per-language corpora. Same content for every eligible model in that language, at least two reference speakers, MP3 and WAV sources, narration, dialogue, numbers, abbreviations, proper names, short headings, long sentences, chapter transitions. Short diagnostics, then 20 to 30 minutes of connected narration, then a full-chapter soak, with repeat generations to expose stochastic failures. Pinned settings and a bounded, equal tuning budget per backend. (TEST-01 opt-in, TEST-04) | L |
| B-6 | Result sheets: `docs/MODEL-EVAL-PL.md` and `docs/MODEL-EVAL-EN.md`. Content fidelity, language quality, voice likeness, long-form listening, practical performance, operational fit, each scored per language and never combined into one number. Samples, settings, errors, timing, chosen default, tested alternatives. | M |
| B-7 | Resolve the model before final chunking. Retain stable source paragraph IDs and derive an engine-specific chunk plan inside that mapping. `char_limit` becomes registry-driven. A model change may require re-chunking the whole book. | M — **done** |
| B-8 | Second and third English candidates once B-4 proves the adapter shape: Qwen3-TTS-12Hz-1.7B-Base, then Chatterbox-Turbo. Qwen's transcript requirement becomes an engine-specific preparation step, not a WhisperX dependency for every voice. | L |

**Portable half done, 2026-09-09.** B-1, B-2, B-3 and B-7 landed together,
because they are one change seen from four places: nothing about a model is
compiled in any more.

- `config/models.toml` holds what each backend narrates, what a reference may
  be in, its fragment limit, native rate, controls and environment. `just
  models` prints it. A malformed entry fails at load rather than mid-render.
- Chunking resolves the backend before splitting, since fragment size is the
  model's property rather than the language's, and snapshots the choice into
  `book.json`. Changing a default tomorrow cannot change a queued book.
- That snapshot is the request. Stage 4 reads it instead of deciding, and
  refuses a book bound to an engine this environment does not implement.
  `narrator/backends/` is the seam, `narrator/choice.py` the mirror, and a
  test compares the mirror against the exported schema so a field added on one
  side and not the other fails rather than being silently dropped.
- The render report now records which model and rate produced the audio.
- Schema 4.

XTTS is unchanged in behaviour; it is simply no longer the only path. The
remaining items need a second backend and a machine to listen on.

**Hardware dependency.** B-5, B-6, and B-8 produce listening and throughput
evidence and cannot be completed on the Mac. They belong on the CUDA box; see
[HANDOFF-GPU.md](HANDOFF-GPU.md). B-1 through B-4 and B-7 are portable. An
advertised CUDA-compatible wheel is not evidence of a successful run.

**Exit evidence.** Polish and English defaults can be set independently, a book
can override either, one saved voice compares across candidates, and eligibility
or rejection is visible with its reason. Both defaults pass the longer narration
checks on the target hardware.

## Slice C — One dependable book — mostly done

Uses B's contracts. The goal is one Polish chapter and one English chapter that
survive interruption and a change of model or reference.

| ID | Work | Size |
|---|---|---|
| C-1 | Voice library record. Stable ID and display name, original MP3 or WAV, selected clean regions, recording language, optional reviewed transcript, source hashes, processing history. Conditioning artifacts are derived per engine, checkpoint, voice revision, and preprocessing version, cached separately. XTTS latents become one derived representation among several. | L — **part**: the cross-engine conditioning cache waits for a second backend (B-4); see below |
| C-2 | Import, probe, select usable speech, optional cleaning, audition, save. Automatic selection for clean single-speaker material; manual region and speaker selection otherwise. Report silence, clipping, unusable regions, transcript mismatch. Longest ASR segment is not a quality criterion. | M — **part**: the probe is done; region and speaker selection waits for B-4 |
| C-3 | Keep references at native quality and prepare them at each engine's required rate. Preserve engine output metadata and resample explicitly at the assembly boundary. 24 kHz stops being a global assumption. | M — **done** |
| C-4 | Fragment fingerprints (ARCH-04). Reuse a WAV only when accepted text, spoken-text preparation version, language, engine, checkpoint, tokenizer, voice revision, effective settings, and renderer version all match, and the file decodes completely. Replaces the filename check at synth.py:242. | M — **done** |
| C-5 | Atomic publication of audio, manifests, QA, and exports. Previews and dry runs live outside the production fragment tree. | M — **done** |
| C-6 | Finalisation gate. Require the current expected fragment set exactly once, in order, with matching fingerprints and readable audio. Completes D-04 and D-05. (FEAT-05) | M — **done** |
| C-7 | Language-aware QA. Group or select ASR by fragment language instead of taking `chunks[0]`. Compare against intended spoken text while keeping links to original spelling and substitutions. Per-language WER and CER thresholds. Distinguish sampled from full coverage. Never suppress a failure or show stale QA as current. | L — **done** |
| C-8 | Per-book cast and effective settings. Carry role speed through the contract and mark unsupported controls explicit rather than accepting them silently. | M — **done** |
| C-9 | Error taxonomy and bounded, targeted retries with visible failures. No automatic model fallback mid-book. (ARCH-06) | M — **done** |

**Started 2026-09-10 with C-4, because slice B created the hazard it closes.**
Switching models became a one-word argument while stage 4 still decided to
reuse audio on nothing but a filename, so changing model and re-running would
have kept every fragment and reported a clean run.

A fingerprint now covers the text, language, model identity, voice, voice
revision and effective settings behind each fragment. Stage 4 reuses a wav only
on a match that also decodes; assembly recomputes the same fingerprint from the
current plan and refuses what disagrees. The ledger is appended per fragment
rather than written at the end, so a render killed at hour six leaves the first
six hours reusable. Re-cloning a voice invalidates its audio, because the
latents are hashed into the revision.

The algorithm exists twice, since the two environments cannot import each
other. Both copies are stdlib-only so one test can load both and compare their
output, including on non-ASCII text where an encoding difference would show
first. Schema 5 carries the fingerprint and the voice that rendered each
fragment.

**Completed 2026-09-10 apart from the cross-engine voice work.** What the rest
turned out to be:

- **C-7** was the defect the assessment named. One transcriber for a whole book
  meant every fragment in the other language was heard by the wrong model and
  reported as a synthesis failure. Fragments are now grouped by their own
  language, each group heard by a transcriber loaded for it, with its own
  threshold. The report says how much of the book it covers and which audio it
  describes, and the dashboard marks one whose audio has since been re-rendered.
- **C-3** was a live bug rather than a future one. Pauses were generated at the
  configured output rate while fragments carry the engine's, so a backend at any
  other rate would have produced a click at every paragraph break or failed to
  concatenate. The rate now follows the fragments and the single resample
  happens once at the end.
- **C-8** was the ineffective role speed. `cast.yml` has carried a speed per
  role since the beginning and nothing read it, so a dialogue voice set faster
  narrated at exactly the same pace. It is now filtered to controls the backend
  implements, folded into the fingerprint, and applied per fragment.
- **C-9**: `retries` was in the config and unread, so a fragment the engine
  fumbled once was a permanent failure. Failures are also categorised, and a run
  whose first few fragments all fail the same way stops rather than grinding
  through a whole book to report the same voice fault ten thousand times.
- **C-5**: every manifest, report and export is now written beside its target
  and renamed in. The export mattered most: a killed assembly left a truncated
  file that plays, has a plausible length, and reads as finished.
- **C-2**: `just probe` judges a recording before anything is cloned from it.
  Cloning costs minutes and a model download, and most reasons a recording will
  not work are visible in the file.

**What waits for a second backend.** C-1's separate conditioning artifact per
engine and revision, and C-2's manual region and speaker selection, are both
shaped by what a second engine actually needs from a reference. Building them
against one engine would be guessing. They unblock with B-4.

**Exit evidence.** Changing language, model, reference, or text invalidates
incompatible output. Interrupt and restart preserve only valid completed work.
Deleting a fragment blocks export. QA states its language, coverage, input
revision, and synthesis preset.

## Slice D — Folder batches (FEAT-03) — done, bar model reuse

Needs C's reliable single-book render first. Unattended batches over an
unreliable renderer multiply the damage.

| ID | Work | Size |
|---|---|---|
| D-11 | Folder scan. Non-recursive by default with explicit recursion, deterministic reviewable order, TXT and EPUB only, unsupported files reported rather than guessed.  | M — **done** |
| D-12 | Import and deduplication. Copy and hash on import, detect exact duplicates, assign stable book IDs, handle identical titles and basenames without overwriting. A re-scan distinguishes an unchanged input from a new source revision. Input files are never renamed or deleted.   | M — **done** |
| D-13 | Durable queue in SQLite owned by Studio, at `data/.studio/queue.db` beside the jobs and locks rather than in a second studio directory. Atomic claims. Sources, manifests, and audio stay on disk. (ARCH-01 subset) | L — **done** |
| D-14 | One GPU workload at a time, counting clone preparation and ASR. Reuse a loaded model where batch order allows; swapping models between books is acceptable. Depends on D-09. | M — **gate done**, reuse deferred and sized below |
| D-15 | Batch review. Filename, title, encoding, detected or overridden language, model, voice or cast, estimated duration, readiness. Bulk defaults with per-book overrides. | M — **done** |
| D-16 | Pause, cancel, retry, and explicit continuation after a failure. An encoding, language, or model exception pauses that book only. Disk preflight before synthesis and before assembly. | M — **done** |
| D-17 | Snapshot resolved language, model, cast, and settings per run so tomorrow's default cannot change a queued or resumed job. (CONF-01)  | S — **done** in slice B: `book.json` carries the resolved model, its settings and the cast |

**Slice D finished 2026-09-10.** The queue now has something driving it. A
worker settles what finished, then claims at most one step and starts it. One
per tick is deliberate: starting everything ready would put five renders on one
graphics card, and the device gate would refuse four of them as *failures*, so
a batch that started them all at once would be worse than one that takes turns.
The dashboard runs a worker while it is open, `just drain` runs one without it,
and `AF_NO_DRAIN=1` turns the dashboard back into a viewer.

A failed step stops its own book and nothing else, and that needed no new
machinery: a step which is not done already blocks what follows it, so the book
stands still with the reason recorded against the step that failed while the
others carry on. Retrying that step is the explicit continuation. Deliberately
*not* pausing the successors, because then retrying the failed step alone would
leave the rest held and the person would have to do it twice.

The reason on a failed step is the last line the stage printed. That is nearly
always the actual cause and is what someone would have scrolled to, so carrying
it onto the queue saves opening twenty logs to find which book needs a look.

Refusals are told apart. Something else holding the book, the voice or the
device is a matter of timing: the step goes back and the next tick takes it. A
bad argument or a full disk needs a person, so the book stops there.

**The disk preflight is deliberately rough.** It sizes what is left to write,
not the whole book, so a resumed render asks for what it still owes. It keeps a
512 MB reserve, because a render that fits exactly leaves a machine with
nowhere to write the log that would say what went wrong. It runs both in the
recipe and at the moment the button is pressed, so the refusal arrives as a
sentence rather than as an exit code in a log twenty seconds later.

**Two faults came out of running the batch screen on real books rather than on
fixtures.** Both would have passed any test written from the code.

`book.json` is written by chunking, so between importing and splitting a book
had a title, an encoding and a language decision on disk that nothing read. The
review showed a bare slug with no encoding and no language, at exactly the
moment those facts decide whether to commit the machine to narrating it. The
dashboard now falls back to the import record, which fixes every page, not just
this one.

Readiness then refused both freshly imported books for having "no voice or
cast". A book carries no cast of its own until chunking fills it in from
`config/cast.yml`, so an empty field meant "not decided yet", not "nobody".
Every book in a batch that was in fact ready was held back. Readiness now knows
about the configured cast, and the row says when the cast is borrowed from it.

**GPU gate done 2026-09-10.** Every action that loads a model onto the device
now says so, and that includes the two easy to forget: `label` and `verify` run
transcription, and `voice` runs labelling on its way to cloning. Leaving those
out is how a batch survives eight hours and then dies out of memory when a
quality check lands beside a render. Nothing inspects free VRAM, because the
answer is stale by the time it is acted on; the device is taken like any other
lock and whoever asks second is told to wait. Work that loads no model still
runs alongside, so assembling one book does not queue behind narrating another.

Two things came out of this. Lock names were not namespaced, so a voice and a
book of the same name shared one lock file and cloning `solaris` could report
the book of that name as already rendering; every lock name now says what it
protects. And a job that took its book lock and was then refused the device
used to leave the book locked, which the next attempt only survived because
`holder` clears a lock naming a job that was never saved. It now gives back
what it took, and the test looks at the lock file rather than at `holder`,
which papered over the leak.

**Reuse of a loaded model is not done and is bigger than this row suggests.**
Each stage runs as its own detached process, so the model is loaded from disk
every time whatever the batch order is; grouping books by model saves nothing
today. Real reuse needs a resident narrator process that outlives one book,
which is an architecture change rather than a scheduling one. Within a single
render the model is already loaded once and the conditioning swapped per voice,
which is where the cost actually was for a multi-voice cast.

**Queue done 2026-09-10.** One queue entry is one stage of one book, not a
whole book. That is what lets a failed chapter split hold back only that book,
and what will give the GPU gate individual workloads to admit one at a time.
Steps of a book run in order and a step that is not `done` blocks what follows,
including a failed or cancelled one: assembling a book whose synthesis was
cancelled produces a truncated audiobook that looks finished. Across books the
order is insertion order, so a twenty-hour render does not stop the next book's
chapter split from starting.

Two things about the claim are worth recording. It opens `BEGIN IMMEDIATE`
rather than the default deferred transaction, because a claim reads which item
is next and then writes that it is taken, and the write lock has to be held
across both. The first attempt at a concurrency test could not tell the two
apart: eight threads, then six separate processes, all claimed cleanly either
way, because expiry runs first and its `UPDATE` happens to take the lock as a
side effect. The test that does discriminate forces the interleaving directly
and skips expiry, so what it pins is the transaction rather than a coincidence
upstream of it that a later tidy-up could remove.

That test also turned up a real fault: building a `Queue` wrote to the database
unconditionally, so opening a page would queue behind whatever claim was in
flight. Creating the tables now takes no write lock once they exist.

**Scan and import done 2026-09-10.** Ingestion was refactored into a callable
so the single-file command and the folder pass share one implementation, and it
now returns its outcome rather than raising: an unreadable file among twenty
must not stop the other nineteen. `just import-folder` reports every file
exactly once, and re-running it imports nothing, because books already here are
recognised by their bytes.

Importing a folder exposed a gap in the decoder. A file that only ISO-8859-2
would accept decodes to a page of control characters, and with nothing
competing there was no ambiguity to flag, so it was imported as a book. Being
the only reading that did not raise is not the same as being right, and a
quality floor now catches it.

**Started 2026-09-10 with the scan.** Classification is the part that can do
damage, so it went first and the order of its questions is the design.
Identical bytes settle it outright, so a book already here reads as
`unchanged` however many copies the folder holds and whatever they are called.
Failing that, the source path recorded at ingestion separates a corrected copy
of a book already here from a different book that happens to share a filename.
Anything left is new and takes a numbered name if a different book holds the
one it wants.

The first version got this wrong: slug disambiguation ran before
classification, so a revised book looked new and would have been imported
alongside the one it was meant to replace.

**Exit evidence.** A folder holding Polish and English TXT and EPUB files, an
ambiguous encoding, a duplicate, and two matching titles completes with separate
outputs per book. Restart during synthesis and re-scan afterwards produce no
duplicate renders, no overwritten sources, and no lost progress.

## Slice E — Listening polish — done

Only after D. Each item reduces manual correction rather than enabling the
workflow. Sized 2026-09-11 against what is already there.

| ID | Work | Size |
|---|---|---|
| E-1 | **Done 2026-09-11.** Pronunciation editor over A-7's dictionary. `speech.load_dictionary` already reads `data/book/<slug>/pronunciation.yml` and chunking already applies it; what is missing is a way to edit it without a text editor, and a way to hear the result before re-chunking a book. (FEAT-01 P1) | M |
| E-2 | **Done 2026-09-11.** Richer auditions with recorded settings. `just voice` renders one fixed sentence at temperature 0.7 and nothing records what produced it. Audition a voice on a passage from the book, at the settings that will actually be used, and keep what was heard. (FEAT-07 remainder) | M |
| E-3 | **Done 2026-09-11.** Loudness matching and clipping control across voices. Nothing exists: `loudnorm` in `config/pipeline.toml` cleans the input recording at clone time and has no bearing on output. A cast whose dialogue voice sits several dB below its narrator is the most audible defect a multi-voice book has. Mastering presets stay optional and separate. (FEAT-11 narrow) | L |
| E-4 | **Done 2026-09-11.** Chapter, title, voice and cover metadata in exports. Assembly already writes title, artist, album and chapter marks. Missing: the narrator as a tag, and cover art, which most EPUBs carry and nothing currently extracts. (FEAT-04 metadata) | S |

**Order.** E-3 first, because it is the one a listener hears and the only one
with nothing behind it. Then E-4, which is small and finishes the export. E-1
and E-2 are both about judging a voice before committing hours to it, and
sharing that shape they are better done together, after the audio itself is
right.

**E-1 and E-2 done 2026-09-11, together, because they turned out to be one
thing.** Both ask the same question: is this worth spending a night of the
machine on. Both answer it by rendering a short sample and keeping what
produced it, so there is one mechanism rather than two.

Previewing a pronunciation is free, and that is the whole argument for it. The
substitution is pure text, so every passage a proposed rule would change can be
shown instantly, with no model and no re-chunking. Deciding whether a fix is
worth re-splitting several thousand fragments for now costs nothing. Rules that
would do nothing are refused outright, because a rule written the same as it is
spoken is a mistake somebody makes once and then hunts for.

Auditions now read a passage from the book, chosen from the middle rather than
the start, preferring one long enough to carry a sentence boundary and matching
the role being judged, so a dialogue voice is heard saying dialogue. The
settings come from `book.json`, through the same backend synthesis uses, so
what is heard is what would be made. Everything that shaped a sample is written
beside it, and samples are named by that, so two settings are two files rather
than one overwriting the other, and asking twice costs nothing.

The audition made while cloning is deliberately left where it is: `just level`
measures it, and moving it would change every voice's correction.

Adding an action that loads a model made the pinned GPU set fail, which is the
test doing its job. Auditions queue behind a render like everything else.

**E-4 done 2026-09-11.** The export is the only thing that survives the
pipeline: everything else lives under `data/` and is read by this program
alone. What a listener sees on their phone is now the narrator and the cover as
well as the title, author and chapter marks that were already there.

The narrator goes in `composer`, which is where audiobook players look for it;
there is no dedicated tag and that is the one the shops settled on. A cast is
named in full rather than reduced to its narrator, because a book read by two
people is read by two people.

Covers are lifted out of the EPUB at import and left beside the text under a
plain name, so a person can replace one they dislike or supply one for a plain
text book that never had any. Three ways an EPUB declares a cover, tried in
order of how definite they are, and each is now tested on its own.

That mattered: the EPUB 2 lookup was reading the wrong element the whole time,
because ebooklib files `<meta name="cover">` under the OPF namespace as `meta`
rather than as `cover`. The filename fallback answered instead and the test
passed. It only showed up when the fallback was removed to check the test bit.

The explicit stream mapping on the ffmpeg call turned out to be defensive
rather than load-bearing; ffmpeg picks correctly without it. The comment says
so now instead of claiming a bug it prevents.

Not verified on a real EPUB, because every book in this library is plain text.
The three declaration styles are each tested against a constructed file, and
the embedding is a real ffmpeg round-trip read back with ffprobe.

**E-3 done 2026-09-11.** A voice is measured from its audition when it is
cloned, in EBU R128 integrated loudness, and the correction that brings it to
the configured target is written into its profile. The narrator applies it while
rendering; nothing else has to know.

The true-peak ceiling is a constraint rather than a preference, so a voice that
is quiet on average but peaks near full scale comes *down* even though that
takes it further from the loudness target. Saying so turned out to matter: the
first version of the report claimed the target had been reached whether or not
the ceiling had let it. It now says where the voice actually lands, and warns
that such a voice will sit below the rest of the cast.

Two of the first tests asserted the wrong numbers, both because I reasoned about
the target and forgot the headroom. The code was right and the expectations were
not, which is the good version of that discovery.

Clipping is counted rather than hidden. The gain is chosen so the audition stays
under the ceiling, but a louder passage in the book can still reach the top, and
a voice that clips is one whose correction is too large. The run report carries
the gains applied and any clipping, because a warning on stderr scrolls past
during a twenty-hour render.

**Where the loudness correction belongs.** Not at assembly. Adjusting levels
between voices has to happen before the fragments are concatenated, and doing
it there means writing adjusted copies of a book's audio into a temporary
directory: several gigabytes of transient disk for a long book, every time it
is exported.

It belongs at the voice. A voice is cloned once, and the audition rendered at
that moment is the model's own output for it, so measuring the audition gives a
per-voice gain that costs nothing to apply during synthesis. Recorded in the
voice profile, carried into the fragment fingerprint, so a level correction is
reproducible and resume stays honest about what was rendered with what.

A voice with no measured level gets no gain and no fingerprint change, so books
already rendered are not invalidated by this landing.

## Cross-cutting

Carried alongside the slices rather than sequenced.

| Item | Trigger |
|---|---|
| AUTH-01, NET-01: session token, bind policy, Origin and Host checks | P1 while the dashboard stays on localhost. P0 the moment it is reachable from the LAN. CORS is not authentication. |
| CI-01 | Extend the existing four-environment CI with the new fixtures and a manual real-model job. No observability platform. |
| DX-04 debug bundle | Report decoding decisions, detector scores, engine and revision, voice cache identity, and failures. Exclude source text and voice audio by default. |
| DX-06 tagging and changelog | Tag known-good versions once slice C lands, so a twenty-hour render can be pinned to one. |
| TEST-02 contract tests | Follows B-1. Validate real engine writer output, encoding and language provenance, model selection, and sample-rate metadata across the environment boundary. |
| SEC-03 dependency audit | Periodic and deliberate. Never relax the XTTS pins to clear a finding. |

## Explicitly not in this plan

Deferred with the assessment's reasoning, recorded here so they are not
rediscovered: AUTH-02 roles, JOB-02 container-per-job, ARCH-03 Redis event bus,
ARCH-02 tracing stack, FEAT-09 emotion UI, FEAT-10 voice blending, FEAT-16
fine-tuning UI, FEAT-17 translation, FEAT-19 podcast mode, FEAT-21 streaming,
FEAT-22 diffusion editing, FEAT-23 emotion detection, DX-01 SPA rewrite,
FEAT-15 external API keys and SDKs, and a wholesale state-store migration.
PDF, MOBI, OCR, and splitting one file into several books remain out of scope.

Fine-tuning stays the open task in [HANDOFF-GPU.md](HANDOFF-GPU.md) and is not
promoted here. The assessment's position holds: prove instant cloning and
reference curation first.

## Decisions needed

1. **First alternative backend.** Recommendation is Chatterbox Multilingual, one
   new environment covering both language comparisons. Confirm or redirect
   before B-4.
2. **CUDA box availability.** B-5, B-6, and B-8 are blocked without it. If it is
   far off, slice B stops after B-7 and slice C proceeds on XTTS alone.
3. **Dashboard reach.** If the dashboard will ever be opened beyond localhost,
   AUTH-01 and NET-01 move into slice 0.
4. ~~**Language detector.**~~ Settled in slice A: `lingua-language-detector`,
   for the reasons recorded there.

## Definition of completion

Unchanged from the assessment. A voice saved from MP3 or WAV; a folder of
independent TXT and EPUB books imported; Polish characters and English
punctuation preserved; encoding and language uncertainty inspectable; separate
Polish and English model defaults chosen; a single book's model and cast
overridable; those choices auditioned; and complete audiobooks verified as
labelled, with interruption recovery.

Completion needs real narration evidence in both languages and at least one
working alternative model path. A model dropdown and a green text-only suite do
not satisfy it.
