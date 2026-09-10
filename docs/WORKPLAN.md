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

## Slice C — One dependable book

Uses B's contracts. The goal is one Polish chapter and one English chapter that
survive interruption and a change of model or reference.

| ID | Work | Size |
|---|---|---|
| C-1 | Voice library record. Stable ID and display name, original MP3 or WAV, selected clean regions, recording language, optional reviewed transcript, source hashes, processing history. Conditioning artifacts are derived per engine, checkpoint, voice revision, and preprocessing version, cached separately. XTTS latents become one derived representation among several. | L |
| C-2 | Import, probe, select usable speech, optional cleaning, audition, save. Automatic selection for clean single-speaker material; manual region and speaker selection otherwise. Report silence, clipping, unusable regions, transcript mismatch. Longest ASR segment is not a quality criterion. | M |
| C-3 | Keep references at native quality and prepare them at each engine's required rate. Preserve engine output metadata and resample explicitly at the assembly boundary. 24 kHz stops being a global assumption. | M |
| C-4 | Fragment fingerprints (ARCH-04). Reuse a WAV only when accepted text, spoken-text preparation version, language, engine, checkpoint, tokenizer, voice revision, effective settings, and renderer version all match, and the file decodes completely. Replaces the filename check at synth.py:242. | M |
| C-5 | Atomic publication of audio, manifests, QA, and exports. Previews and dry runs live outside the production fragment tree. | M |
| C-6 | Finalisation gate. Require the current expected fragment set exactly once, in order, with matching fingerprints and readable audio. Completes D-04 and D-05. (FEAT-05) | M |
| C-7 | Language-aware QA. Group or select ASR by fragment language instead of taking `chunks[0]`. Compare against intended spoken text while keeping links to original spelling and substitutions. Per-language WER and CER thresholds. Distinguish sampled from full coverage. Never suppress a failure or show stale QA as current. | L |
| C-8 | Per-book cast and effective settings. Carry role speed through the contract and mark unsupported controls explicit rather than accepting them silently. | M |
| C-9 | Error taxonomy and bounded, targeted retries with visible failures. No automatic model fallback mid-book. (ARCH-06) | M |

**Exit evidence.** Changing language, model, reference, or text invalidates
incompatible output. Interrupt and restart preserve only valid completed work.
Deleting a fragment blocks export. QA states its language, coverage, input
revision, and synthesis preset.

## Slice D — Folder batches (FEAT-03)

Needs C's reliable single-book render first. Unattended batches over an
unreliable renderer multiply the damage.

| ID | Work | Size |
|---|---|---|
| D-11 | Folder scan. Non-recursive by default with explicit recursion, deterministic reviewable order, TXT and EPUB only, unsupported files reported rather than guessed. | M |
| D-12 | Import and deduplication. Copy and hash on import, detect exact duplicates, assign stable book IDs, handle identical titles and basenames without overwriting. A re-scan distinguishes an unchanged input from a new source revision. Input files are never renamed or deleted. | M |
| D-13 | Durable queue in SQLite owned by Studio, at `data/studio/queue.db`. Atomic claims. Sources, manifests, and audio stay on disk. (ARCH-01 subset) | L |
| D-14 | One GPU workload at a time, counting clone preparation and ASR. Reuse a loaded model where batch order allows; swapping models between books is acceptable. Depends on D-09. | M |
| D-15 | Batch review. Filename, title, encoding, detected or overridden language, model, voice or cast, estimated duration, readiness. Bulk defaults with per-book overrides. | M |
| D-16 | Pause, cancel, retry, and explicit continuation after a failure. An encoding, language, or model exception pauses that book only. Disk preflight before synthesis and before assembly. | M |
| D-17 | Snapshot resolved language, model, cast, and settings per run so tomorrow's default cannot change a queued or resumed job. (CONF-01) | S |

**Exit evidence.** A folder holding Polish and English TXT and EPUB files, an
ambiguous encoding, a duplicate, and two matching titles completes with separate
outputs per book. Restart during synthesis and re-scan afterwards produce no
duplicate renders, no overwritten sources, and no lost progress.

## Slice E — Listening polish

Only after D. Each item reduces manual correction rather than enabling the
workflow.

- E-1 Pronunciation editor over A-7's dictionary. (FEAT-01 P1)
- E-2 Richer auditions with recorded settings. (FEAT-07 remainder)
- E-3 Loudness matching and clipping control across voices. Mastering presets stay optional and separate. (FEAT-11 narrow)
- E-4 Chapter, title, voice, and cover metadata in exports. (FEAT-04 metadata)

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
