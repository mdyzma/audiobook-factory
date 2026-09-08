# Roadmap assessment: Polish and English audiobooks from a folder

Rewritten 2026-09-08 to reflect the clarified goal. Code baseline: `a265f90`. This assesses [ROADMAP-IMPROVEMENTS.md](ROADMAP-IMPROVEMENTS.md); all 59 original proposals are reprioritized below.

## Product goal and priorities

Build a local application that saves cloned voices from MP3/WAV recordings, reads TXT/EPUB books, and processes multiple book files from a selected folder. The main investment should go into **correct text decoding and preparation, reliable language detection, and choosing the best validated synthesis model separately for Polish and English**.

One file represents one book. PDF, MOBI, OCR, other ebook-format expansion, and splitting one file into several books are outside this roadmap. Folder processing means an explicit scan and batch submission; continuous folder monitoring is not required.

“Text coding” is interpreted here as character encoding plus preparing decoded text for narration. These are distinct from detecting whether the text is Polish or English. Both stages must work before choosing a synthesis model.

The target workflow is:

**Select folder → inspect TXT/EPUB files → decode and extract text → detect language → review exceptions → select model and voice/cast per book → audition → queue → verify and repair → export.**

| Rank | Workstream | Why it matters |
|---|---|---|
| 1 | Preserve bytes, characters, text, and reading order | No model can recover letters or passages already lost during import. |
| 2 | Detect and confirm Polish/English correctly | Language determines pronunciation, text preparation, model eligibility, and verification. |
| 3 | Compare and select synthesis models per language | Polish quality and English quality are independent decisions; one shared model is optional. |
| 4 | Make the voice library usable across those models | A saved voice needs reusable reference audio, with model-specific conditioning derived from it. |
| 5 | Guarantee correct, resumable rendering | Changing a model, language, voice, or text must never reuse incompatible old audio. |
| 6 | Process a folder reliably | Each file has its own language, model, cast, status, and output; one bad file must not derail the batch. |

These are priorities for product value, not six isolated projects. Begin the model comparison during the text/language work, and establish render correctness before enabling unattended batches.

Keep the current separation between text work, synthesis, transcription, and the dashboard. Retain the existing XTTS environment as a baseline. Add isolated environments for new synthesis backends when their dependencies require it; the number of environments should follow compatibility, not remain fixed at four.

## 1. Text encoding and spoken-text preparation — P0

### Decode without destroying evidence

The current TXT importer uses `read_text(encoding="utf-8", errors="replace")`. A legacy-encoded Polish book can therefore lose characters before language detection or synthesis sees them. `ftfy` cannot reliably reconstruct bytes already replaced with `�`. [Current importer](../apps/bookbinder/src/bookbinder/ingest.py), line 150.

Use a deterministic decoding workflow:

1. Preserve the original bytes and their SHA-256 hash. Import a stable copy so later edits to the input folder cannot change a queued book.
2. Honor an explicit encoding override and report conflicts with a byte-order mark. Otherwise inspect BOMs, attempt strict UTF-8, then evaluate likely legacy encodings.
3. Include UTF-8 with/without BOM, UTF-16 with BOM, Windows-1250, ISO-8859-2, and Windows-1252 in the initial test set. ASCII-only text can have several equivalent encodings; do not invent certainty where the resulting text is identical.
4. Use a detector such as `charset-normalizer` to rank ambiguous candidates, followed by text-quality checks and representative previews. A successful single-byte decode does not establish the correct encoding. Detector scores are evidence, not a calibrated probability of correctness. [Detector result semantics](https://charset-normalizer.readthedocs.io/en/latest/user/handling_result.html).
5. If ambiguity changes the decoded content, mark that file `needs_review`; show candidate text and an encoding override. Do not silently continue with replacement characters or use an `ignore` error mode. Other ready files can proceed.
6. Save the accepted encoding, selection method, diagnostic scores, warnings, and decoder version. Store accepted text internally as UTF-8 with a consistent Unicode normalization policy, preferably NFC.

Keep EPUB as a structured container: parse its declared XML/XHTML encodings and metadata through the EPUB parser. Do not run a whole-file text decoder over the ZIP bytes. Flag malformed declarations or suspicious decoded content in individual documents.

### Preserve Polish spelling and meaningful structure

Keep three representations: original source, decoded/extracted text, and text prepared for speech. Record transformations and source anchors between them. Display names and prose must retain `ą ć ę ł ń ó ś ź ż` and their uppercase forms; filename slugification must remain separate.

Fix the already identified EPUB spine-order error, nested-block duplication, omitted text-only containers, lost TXT preamble, and overlong-token truncation. Preserve one-character paragraphs where meaningful. De-hyphenate line wraps before discarding line boundaries; the current `_paragraphs()` order removes newlines before `normalise()` can match its de-hyphenation pattern. Preserve real compound-word hyphens and scene/dialogue boundaries. [Ingestion](../apps/bookbinder/src/bookbinder/ingest.py), [chunking](../apps/bookbinder/src/bookbinder/chunk.py).

Make footnote/front-matter exclusions explicit and reviewable. Add tests for Polish abbreviations and punctuation (`dr.`, `prof.`, `np.`, `itd.`), English abbreviations and possessives, curly quotation marks, nonbreaking spaces, and soft hyphens. The installed `pysbd` includes Polish and English rules; extend verified behavior rather than assuming the splitter must be replaced.

### Prepare text in the detected language

Language-specific text preparation should handle dates, numbers, units, currencies, acronyms, and a per-book pronunciation dictionary. Context matters: `1,5`, `1.5`, `1 234,56 zł`, and an ambiguous date are not interchangeable. Polish number expansion can depend on grammatical context. Favor conservative rules and previewable corrections over broad replacements that change meaning.

Apply substitutions before final model token-budget validation. Keep any model-specific normalization inside the backend preparation step; do not permanently alter the shared source text to suit one model. If a backend already verbalizes an expression well, avoid expanding it twice.

Acceptance: the same Polish text in UTF-8, UTF-16, Windows-1250, and ISO-8859-2 produces identical accepted Unicode text after the correct encoding is selected. Ambiguous/corrupt inputs are surfaced without text loss. Re-import and normalization preserve all intended passages once, in order. Tests cover Polish diacritics, English punctuation, mixed line endings, chapter headings, and numbers.

## 2. Language detection is a pipeline decision — P0

Current TXT ingestion defaults to `pl`; EPUB primarily relies on metadata. Studio does not expose a book-language argument in its ingest action. Synthesis uses manifest language, and verification takes the first fragment's language for the whole run. This is insufficient for a folder containing both English and Polish books. [Ingest](../apps/bookbinder/src/bookbinder/ingest.py), [Studio actions](../apps/studio/src/studio/jobs.py), [verification](../apps/transcriber/src/transcriber/verify.py).

Detect language from accepted text, after decoding quality checks and before language-specific spoken-text normalization. Sample substantial passages from the beginning, middle, end, and chapter bodies; exclude titles, navigation, and boilerplate where possible. Cross-check EPUB metadata against the content instead of treating metadata as authoritative.

Start with a small CPU language detector and measure it against the project corpus. `fastText lid.176` is one concrete candidate: it covers Polish and English, expects UTF-8, and can identify other languages so they are not forced into a binary choice. Check binding compatibility before adding it to the text environment. [fastText language identification](https://fasttext.cc/docs/en/language-identification.html).

Decision rules:

- An explicit user override wins and remains saved. Keep conflicting detector evidence visible.
- Agreeing substantial samples can select `pl` or `en` automatically using a threshold calibrated on the corpus.
- Short, ambiguous, damaged, unsupported-language, or materially conflicting samples yield `needs_review`, not a default to Polish.
- A Polish book quoting a short English phrase should normally retain its book language. Offer chapter/passage overrides for sustained language changes; do not switch models on individual foreign names.
- Keep reference-recording language, book language, optional passage language, synthesis language, and ASR language as separate fields.

Persist the detector/version, sample locations, scores, metadata language, chosen language, override source, and coverage. Calibrate detection with difficult cases, including English front matter in a Polish book, incorrect EPUB metadata, short books, and German text outside the supported product scope. Do not advertise detector scores as accuracy guarantees.

Acceptance: a mixed Polish/English folder routes each book correctly; questionable items pause individually. A manual correction changes subsequent preparation, eligible models, chunking, synthesis, and QA. Foreign-language quotations do not cause uncontrolled model/voice switching.

## 3. Choose the best validated model for each language — P0

### Support selection before declaring a winner

The existing implementation is XTTS-specific: `engine.py` hardcodes its model, `VoicePool` holds one model instance, Studio derives language choices from XTTS limits, and chunk limits live in a global XTTS table. A new model choice requires a small backend contract, not just another dropdown. [Engine](../apps/narrator/src/narrator/engine.py), [pool](../apps/narrator/src/narrator/synth.py), [manifest](../apps/bookbinder/src/bookbinder/manifest.py).

Maintain a model registry with supported narration/reference languages, reference-audio/transcript requirements, voice-cloning support, token/context limits, native audio rate, controls, runtime environment, exact checkpoint/tokenizer revision, and validation status. Voice language compatibility must be checked for the selected model, including cross-language references.

Resolve the model as: explicit book/role override → validated default for the book language. A role override is optional advanced functionality; the first usable version needs one model per book with multiple voices. Polish and English defaults may point to different models. Validate every override; never silently substitute a different model during a run. If the selected model is unavailable or unsuitable, show the reason and alternatives before queuing.

A named default points to a pinned, tested preset. Changing tomorrow's default must not change a queued or resumed run. Do not assume equal numeric speed/temperature settings mean the same thing across engines.

### Initial shortlist, checked against primary sources on 2026-09-08

This is a bounded comparison set, not a claim that these are the best models available or that any candidate has already won on this project.

| Candidate | Polish | English | Decision |
|---|---|---|---|
| XTTS-v2 | Documented support | Documented support | Keep as the working integration baseline and benchmark both languages. Existing integration reduces setup work, not the need to assess quality. [Model card](https://huggingface.co/coqui/XTTS-v2). |
| Chatterbox Multilingual | Explicitly lists `pl` | Explicitly lists `en` | Primary challenger for Polish; also include in English comparison. Pin the exact available multilingual release and verify it locally. [Official repository and languages](https://github.com/resemble-ai/chatterbox#supported-languages). |
| Qwen3-TTS-12Hz-1.7B-Base | Not in the documented language list | Documented support and reference voice cloning | Primary English challenger. Use the **Base** cloning variant; CustomVoice and VoiceDesign serve different workflows. Do not route Polish into it based on a general “multilingual” label. [Exact model card](https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-Base). |
| Chatterbox-Turbo | Not documented for Polish | Documented English reference voice cloning | Include as an English throughput/quality candidate. Its low-latency focus does not prove it produces the most consistent audiobook. [Official variants and example](https://github.com/resemble-ai/chatterbox). |

Recommended evaluation order: **Polish: XTTS versus Chatterbox Multilingual. English: XTTS, Qwen3-TTS Base, and the Chatterbox candidates.** Keep the candidate count small until these comparisons reveal a concrete gap. No new backend was installed or benchmarked for this rewrite.

For Qwen's reference-cloning workflow, the normal prompt includes reference audio and its transcript; its embedding-only mode removes the transcript requirement with a documented possible quality tradeoff. Make transcription an engine-specific preparation step instead of requiring WhisperX for every voice/model. [Qwen cloning API](https://github.com/QwenLM/Qwen3-TTS#voice-clone).

### Make “best” an audiobook acceptance result

Build separate Polish and English corpora with the same content used for all eligible models in that language. Include at least two reference speakers, MP3/WAV sources, narration, dialogue, numbers, abbreviations, proper names, short headings, long sentences, and chapter transitions. Use controlled clean references first; evaluate noisier and cross-language references as separate cases.

First run short diagnostic passages; then advance viable candidates to 20–30 minutes of connected narration per language and a full-chapter soak. Include repeat generations for selected passages to expose stochastic failures. Pin versions/settings and give each backend a bounded, comparable tuning budget; comparing one tuned engine against another's defaults is weak evidence.

| Criterion | What to measure | Selection rule |
|---|---|---|
| Content fidelity | Missing/repeated clauses, words at chunk boundaries, hallucinated continuations; WER/CER plus manual review | Reject persistent content failures before considering speed. |
| Language quality | Polish diacritics, inflection, stress, numbers; English pronunciation, contractions, names, accent consistency | Evaluate independently for each language with a fluent listener. |
| Voice likeness | Blind comparison with the reference; identity consistency across passages | Reference cloning must remain recognizable, not merely intelligible. |
| Long-form listening | Prosody, stable identity/loudness, pauses, fatigue, chapter continuity | Short demos alone cannot establish audiobook suitability. |
| Practical performance | Cold start, reference preparation, audio seconds per wall second, peak VRAM/RAM, failures, and QA overhead | Among quality-passing candidates, favor feasible runtime on the target machine. |
| Operational fit | Offline loading after download, pinned assets, interruption recovery, backend setup and reference requirements | Must integrate reproducibly with the local workflow. |

Do not combine Polish and English into one score that hides a weak language. Publish a per-language result sheet with audio samples, settings, errors, timing, and a selected default plus tested alternatives. Thresholds should be agreed from baseline listening before selecting a winner; do not fabricate numerical quality guarantees.

Model choice can also depend on the reference speaker and book. Allow a book-specific override and compare previews of actual book passages. If the same voice must read both languages, audition both language/model combinations: sharing the reference does not guarantee identical timbre or accent across engines.

Acceptance: the user can independently set Polish and English defaults, override a book, compare candidates using one saved voice, and see why a model is eligible or rejected. Both chosen defaults pass the longer narration checks on the actual target hardware.

## 4. Reusable voices across synthesis backends — P0

The durable library asset is the selected reference recording and its metadata. XTTS latents are only one derived representation; they cannot be passed directly to Qwen or Chatterbox.

Keep a stable voice ID/display name, original MP3/WAV, selected clean regions, recording language, optional reviewed transcript, source hashes, and processing history. Derive separate conditioning artifacts by engine/checkpoint, voice revision, preprocessing version, and any language-dependent preparation. Show which model/language combinations have been auditioned successfully.

Provide import → probe → select usable speech → optional cleaning → audition → save. Use clean single-speaker material automatically when suitable; offer manual region/speaker selection for recordings with more than one voice. Feedback should identify silence, clipping, unusable regions, or a transcript mismatch. Selecting only the longest ASR segments is not a sufficient quality criterion.

Keep original/native-quality references and prepare them at each engine's required rate. The existing 24 kHz intermediate format is an XTTS convention, not a universal input constraint. Preserve each engine's native output metadata; resample explicitly at the assembly boundary if needed. Sample-rate mistakes must never change pitch or chapter timing.

Give each book its own cast and effective settings. Carry role speed through the contract and make unsupported controls explicit. Mixed narration/dialogue corrections remain useful; fully automatic character attribution is not required for initial multi-voice narration.

Acceptance: import once, restart, and reuse the same library voice in two eligible engines without losing the reference. Changing a transcript/reference invalidates the appropriate conditioning and render artifacts. A cast never accidentally uses the first loaded checkpoint for every voice.

## 5. Reliable rendering and language-aware QA — P0

Reuse a fragment only if its fingerprint matches accepted text, spoken-text preparation, language, engine/checkpoint/tokenizer, voice revision, effective settings, and renderer version. Validate that the cached WAV is complete and decodable. Current synthesis skips any existing WAV, so selecting a new model would otherwise appear to succeed while retaining old speech. [Resume logic](../apps/narrator/src/narrator/synth.py), line 242.

Resolve the model before final chunking. Retain stable source paragraph IDs; derive engine-specific chunk plans within that source mapping. Token budgets, sentence splitting, and context handling are backend capabilities. Avoid forcing every engine into XTTS's small character limits, and never truncate text to make a chunk fit. A model change may require re-chunking and re-rendering the book, not a minimal fragment patch.

Write audio, manifests, QA, and exports atomically. Keep previews and dry runs separate from production fragments. Before finalization require the current expected fragment set exactly once, in order, with matching fingerprints and readable audio. Assembly currently skips missing files, and Studio may label a book `done` based solely on an output's existence. Those behaviors must be corrected. [Assembly](../apps/bookbinder/src/bookbinder/assemble.py), [state](../apps/studio/src/studio/data.py).

Use all-fragment integrity/silence/clipping checks and language-appropriate ASR. Distinguish sampled QA from full ASR coverage, and retain listening checks for likeness and prosody. Verification of a mixed-language project must group or select ASR by fragment language. Compare against the intended spoken text while retaining links to the original spelling and substitutions. Tune WER/CER thresholds by language and expression type; ASR mistakes and alternative number spellings are not automatically TTS failures.

Record bounded retries and failures. Do not suppress failed verification in the CLI or show stale QA as current. A failed model must not trigger automatic fallback midway through a book and silently change its voice.

Acceptance: changing language, model, reference, or text invalidates incompatible output. Interrupt/restart preserves only valid completed work. Deleting a fragment blocks final export. QA identifies its language, coverage, input revision, and synthesis preset.

## 6. Multiple book files from a folder — P0

Scan a selected local folder for TXT/EPUB. Each file becomes one independent book. Default to a non-recursive scan with explicit recursion and a deterministic, reviewable order. Ignore unrelated files and report unsupported ones; do not treat unknown binary files as TXT.

The batch review shows filename, title, encoding where applicable, detected/overridden language, model, voice/cast, estimated duration, and readiness. Support bulk defaults with per-book overrides. One folder may contain an English EPUB using one engine and a Polish legacy-encoded TXT using another.

Copy/hash inputs during import, detect exact duplicates, assign stable book IDs, and handle identical titles/basenames without overwriting. A re-scan should distinguish unchanged input from a new source revision. Keep original input files untouched; no rename or deletion is needed to mark completion.

Use a small durable queue, preferably SQLite owned by the orchestrator, while preserving file manifests/audio. Claim jobs atomically and start with one GPU workload at a time, including clone preparation and ASR. Reuse a loaded model when feasible, but honor batch order and memory limits; swapping models between books is acceptable. Current lock acquisition and overlapping stage jobs need correction before this is safe. [Job supervision](../apps/studio/src/studio/jobs.py).

Persist queue order, imported source revision, resolved language/model, cast, settings, and job states. Support pause/cancel/retry and explicit continuation after an item fails. Encoding/language/model exceptions pause that book, while independent ready books continue according to the queue policy. Check disk space before expensive work and before assembly.

Acceptance: process a folder containing Polish and English TXT/EPUB files, an ambiguous encoding, a duplicate, and two matching titles. Each ready book gets its selected model/voice and separate output. Restart during synthesis and re-scan afterward without duplicate renders, overwritten sources, or lost progress.

## Delivery sequence and missing roadmap work

P0 is required for the stated workflow. P1 improves everyday use. P2 depends on demonstrated need. P3 is outside this milestone. Research/model comparisons are bounded implementation work, not an open-ended search for an eternally best model.

| Slice | Deliverable and dependency | Exit evidence |
|---|---|---|
| A: text and language | Strict decoding, text preservation, automatic PL/EN detection, metadata checks, overrides; start the benchmark corpus | Equivalent accepted text across encoding fixtures; correct routing or explicit abstention on language fixtures |
| B: model selection | Backend contract/registry, XTTS baseline plus shortlisted adapters, per-language comparison, model-specific chunking and voice preparation; uses accepted text from A | Separate Polish and English result sheets, audible samples, pinned presets, and working override controls |
| C: one dependable book | Cross-model voice library, book-specific cast, fingerprinted resume, complete assembly, language-aware QA; uses B's contracts | One Polish and one English chapter complete reliably, including interruption and changes of model/reference |
| D: folder batches | Safe import/deduplication, immutable per-book settings, atomic queue claims, failure isolation; uses C's reliable render | Mixed-language folder completes with independent outputs and recovery after restart |
| E: listening polish | Pronunciation editor, richer auditions, loudness matching, chapter/cover metadata | Fewer manual corrections and consistent listening across selected voices |

The original roadmap under-specifies three work items that should now be explicit: **TEXT-01: encoding and text preservation; LANG-01: reliable Polish/English language decisions; MODEL-01: backend selection and per-language audiobook evaluation.** FEAT-03 should become **folder-to-library batch processing**. Model evaluation should begin early, rather than waiting for XTTS to fail or treating another engine as a distant plugin experiment.

## What to retain and what to challenge in the original roadmap

Keep the existing file contracts, reports, voice auditions, role corrections, fragment repair, real ffmpeg tests, and isolated dependencies. A narrow engine request/result schema is needed now; a wholesale state-store migration, web-framework replacement, Redis event bus, RBAC system, and tracing platform are not.

The earlier review found useful defects that remain relevant: EPUB order/duplication/omission, lost TXT preamble, long-token truncation, global cast settings, ineffective per-role speed, destructive failed re-upload, startup lock races, stale audio reuse, incomplete assembly, and one-checkpoint reuse across different voice models. These are implementation findings, not reasons to delay the new language/model priorities behind generic infrastructure.

Several roadmap assertions also overstate missing functionality: upload limits and extension validation, filename checks, schema versions, atomic progress, security tests, and structural integration tests already exist. Address remaining path containment, staged import, and command-escaping gaps with focused fixes. Model-style parameters and batching must be checked per backend; they cannot be promised generically. Character-count estimates and comparisons with a stochastic golden waveform are unsuitable universal quality gates.

The roadmap contains 59 unique proposal IDs. Its original phase tables list 60 rows, including a separate API extension, and its summary counts/dependencies disagree. Replace calendar promises with the delivery evidence above.

## Disposition of every original proposal

The revised priority applies to the useful subset described in the last column. “P3” means defer from this product milestone, not that the idea could never be valuable.

### Security and job execution

| ID | Original → revised | Value and scope decision |
|---|---|---|
| AUTH-01 | P0 → P1; P0 for LAN | Small local session/token mechanism and request-origin protection; required authentication for LAN. Avoid a local password/user system initially. |
| AUTH-02 | P1 → P3 | No stated multi-user requirement. Shipping nominal roles all defaulting to admin does not add useful authorization. |
| PATH-01 | P0 → P0, narrow | Retain existing name checks; fix symlink/canonical containment for reads and writes. Use path-relative containment, not string `startswith`. |
| PATH-02 | P0 → P0, narrow | Atomic staged imports, collision protection, audio probes, and EPUB decompression bounds. Restrict book discovery to TXT/EPUB; reject unknown types explicitly. |
| CMD-01 | P0 → P0, narrow | Test/fix `just` shell interpolation and ffmpeg concat/metadata escaping. Preserve valid filenames; do not ban normal punctuation in user paths. |
| NET-01 | P1 → P1; P0 for LAN | Bind policy, Origin/Host checks and session protection matter. CORS is not authentication or CSRF protection; per-IP rate limits are secondary locally. |
| SEC-01 | P2 → P2 | Restricted serving and sensible filesystem permissions now; application-managed encryption/FUSE later. Do not add public unauthenticated exports by default. |
| SEC-02 | P1 → P0 model identity / P1 broader hardening | Exact checkpoint/tokenizer revisions and asset hashes are essential to model comparison, compatible voice caches, and reproducible narration. |
| SEC-03 | P1 → P1 | Lightweight dependency audit and deliberate updates. Preserve isolated locks; do not blindly relax old ML pins to clear findings. |
| JOB-01 | P1 → P0, rewrite | Atomic claiming, one GPU worker, cancellation/recovery, and disk preflight. Detached jobs and persisted PIDs already exist. |
| JOB-02 | P3 → P3 | Container-per-job tenancy and Docker orchestration do not help the personal workflow enough to justify complexity. |

### Testing

| ID | Original → revised | Value and scope decision |
|---|---|---|
| TEST-01 | P0 → P0, incremental | Preserve fast tests; add opt-in real-model Polish/English comparison and longer narration checks. No fictional tiny XTTS checkpoint or mandatory nightly GPU service. |
| TEST-02 | P0 → P0 | Validate actual engine-writer output, encoding/language provenance, model selection, and sample-rate metadata across the environment boundary. |
| TEST-03 | P0 → P0 | Prioritize encoding fixtures, Polish diacritics, sentence boundaries, EPUB order, long tokens, and text preservation. Compare intentional transformations explicitly. |
| TEST-04 | P1 → P0 | Measure content fidelity, voice likeness, language-specific pronunciation, continuity, and all-fragment integrity. Use separate Polish/English acceptance results. |
| TEST-05 | P0 → P0, incremental | Add the reproduced failures to existing suites. Keep environment/model checks separate; do not mutate dependencies in routine unit tests. |
| TEST-06 | P0 → P0, narrow | Expand existing traversal/upload tests with symlinks, collisions, command escaping, and concurrent claims. SQL/NoSQL injection tests are premature without those query paths. |
| TEST-07 | P2 → P0 folder recovery / P2 load | Test folder re-import, duplicates, per-file failure, restart, and GPU admission. Fifty-user load testing is outside the current need. |
| CI-01 | P1 → P1 | Extend the existing four-environment CI with targeted checks and a manual real-model job; avoid a large observability/coverage services project. |
| TEST-08 | P1 → P0 core corpus / P1 helpers | Curated Polish/English encoding and narration fixtures directly drive the priority work. Reuse existing fixture infrastructure. |

### Architecture and infrastructure

| ID | Original → revised | Value and scope decision |
|---|---|---|
| ARCH-01 | P1 → P0 queue subset / P2 full migration | Use SQLite for durable folder batches and atomic job claiming. Keep source files, book manifests, and audio on disk. |
| ARCH-02 | P1 → P1 logs / P3 tracing stack | Structured error context, book/run/chunk IDs, timing. Existing files/logs are useful; Jaeger/Prometheus/OpenTelemetry are excessive for this scope. |
| ARCH-03 | P2 → P3 | Polling or later SSE is enough. Redis/watchers do not solve distributed file/model storage or durable job scheduling by themselves. |
| ARCH-04 | P1 → P0 | Fingerprint text preparation, language, engine/checkpoint, voice revision, and settings; publish atomically. A completion marker alone is insufficient. |
| ARCH-05 | P1 → P0 small engine contract / P2 full extraction | Define a versioned backend-neutral request/result contract now. Migrating all existing models into a shared Python package can wait. |
| ARCH-06 | P1 → P0, narrow | Clear error categories, bounded targeted retries, and visible failures. Reuse reports/queue records; no separate dead-letter file hierarchy needed initially. |
| DOCKER-01 | P2 → P2; P0 if chosen runtime | Fix functional container orchestration and prove CUDA only if Docker is the deployment target. Existing per-environment images need verification, not rediscovery. |
| CONF-01 | P1 → P0 | Make encoding, language, per-language model defaults, per-book overrides, voice/cast, and effective settings explicit and snapshot them per run. |
| ARCH-07 | P2 → P0 new-backend dependency isolation / P3 container mandate | New engines get separate pinned environments where needed. Retain the existing XTTS pins; containerizing everything is not required. |

### Product features

| ID | Original → revised | Value and scope decision |
|---|---|---|
| FEAT-01 | P0 → P0 basic PL/EN text preparation / P1 editor | Language-specific number/abbreviation handling and pronunciation substitutions support correct narration. Validate after transformation; avoid unverified phoneme promises. |
| FEAT-02 | P1 → P0 | Automatic Polish/English detection, metadata conflict checks, confidence/abstention, manual override, and language-aware synthesis/ASR are central requirements. |
| FEAT-03 | P1 → P0 | Explicit folder scan, one TXT/EPUB file per book, duplicate handling, individual language/model/cast, and durable queue recovery. |
| FEAT-04 | P1 → P1 metadata / P3 new formats | Retain M4B/MP3/WAV. Improve chapter/title/voice metadata as needed; additional format expansion has no current product value. |
| FEAT-05 | P0 → P0, rewrite | Block incomplete/stale finalization; expose sampled versus full QA; repair flagged fragments. No dependency on migrating all state. |
| FEAT-06 | P1 → P1 basic / P2 event sourcing | A saved correction history is useful. One cast backup already exists. Full undo/redo event sourcing is optional. |
| FEAT-07 | P1 → P0 | Compare candidate engines on the same book excerpts and reference voice. Isolate previews and record settings; choose models before long renders. |
| FEAT-08 | P1 → P2 | Extend the existing flagged-fragment review. Requiring manual approval of every passage undermines unattended book batches. |
| FEAT-09 | P2 → P3 | Defer an emotion-control UI. Model capabilities differ; support only explicit, validated controls rather than pretending all backends behave alike. |
| FEAT-10 | P2 → P3 | Voice blending/ageing is experimental and unnecessary for importing/selecting real reference voices. |
| FEAT-11 | P1 → P1, narrow | Match loudness and control clipping across voices. Full compression/noise gating and distributor mastering should be separate optional presets. |
| FEAT-12 | P2 → P2 | Benchmark throughput including voice preparation and QA. Add engine-specific batching only after correctness; begin with one GPU job at a time. |
| FEAT-13 | P2 → P2 pause subset / P3 full SSML | Explicit pauses can help. Defer a general annotation language and unsupported emotion/phoneme controls. |
| FEAT-14 | P1 → P0 extraction boundaries / P1 chapter editing | Preserve TXT/EPUB chapter order and sensible sentence boundaries. Editing chapters within a book is useful; splitting a file into several books is excluded. |
| FEAT-15 | P1 → P2 | JSON routes and OpenAPI already exist. Add queue/voice endpoints for the UI; external API keys, versioned SDKs, and clients can wait. |
| FEAT-16 | P2 → P3 | Improve reference curation and instant cloning first. Fine-tuning is not a guaranteed quality upgrade; prove its value before building training UI. |
| FEAT-17 | P3 → P3 | Translation changes the content and creates a new quality problem outside the request. |
| FEAT-18 | P3 → P0 model adapters / P3 plugin ecosystem | Built-in selectable synthesis backends are now core. Add capabilities and language routing; defer dynamic discovery, third-party plugins, and a marketplace. |
| FEAT-19 | P3 → P3 | Podcast feeds, music, ads, and serialization are outside the audiobook workflow. |
| FEAT-20 | P3 → P2 pacing subset / P3 reactive dialogue | Correct speaker assignment is needed now; overlaps and inferred emotional reactions are separate speculative work. |
| FEAT-21 | P3 → P3 | Live low-latency text streaming is a different operating model from producing book files. |
| FEAT-22 | P3 → P3 | Diffusion speech editing is research, while fragment re-rendering already addresses corrections. |
| FEAT-23 | P3 → P3 | Needs data and a controllable backend; offers little before basic voice fidelity/content preservation are reliable. |
| FEAT-24 | P3 → P2 if read-along is wanted | Preserve source anchors now. Reader synchronization and EPUB media overlays are optional products beyond audio export. |

### Developer experience

| ID | Original → revised | Value and scope decision |
|---|---|---|
| DX-01 | P2 → P1 focused UI / P3 SPA rewrite | Add encoding preview, detected language, per-language model defaults, engine auditions, and folder batch status to the current UI. |
| DX-02 | P1 → P0 essential options / P1 CLI polish | Expose folder input, recursion, encoding/language overrides, model choice, and library voice IDs through existing command conventions. |
| DX-03 | P1 → P1 | Install only selected synthesis backends and optional reference transcription/QA. Verify each backend on the target hardware without relaxing the legacy XTTS pins. |
| DX-04 | P1 → P1 | Report decoding decisions, detector scores, chosen engine/revision, voice cache identity, and failures. Exclude source text and voice audio by default. |
| DX-05 | P2 → P2 | Update stale factual guidance and record new decisions. Migrating every old note into an ADR framework is low priority. |
| DX-06 | P2 → P1 lightweight / P2 packaging | Basic tagged known-good versions, changelog, and compatibility notes help long renders. Projects already have version `0.1.0`; release work does not require Docker. |

## Minimal architecture

```text
Folder of TXT/EPUB files
    → immutable source import
    → decoding + extraction + language decision
    → per-book model + voice/cast selection
    → language/model-specific text preparation and chunk plan
    → durable queue → isolated synthesis backend
    → language-aware QA and targeted repair
    → complete audiobook per input file

Voice library: reference audio + language + optional transcript
    → separate conditioning cache for each model/revision

Model defaults: Polish → validated Polish preset
                English → validated English preset
```

Studio and CLI should share the same selection rules and preflight checks. Keep a small queue database, portable source/manifests/audio, and explicit backend boundaries. Add a new isolated runtime only where dependency compatibility requires it. If the documented RTX 5090 is the intended machine, validate exact wheels, model loading, decoding, inference, and memory behavior there; an advertised CUDA-compatible package or image is not evidence of a successful run.

## Definition of completion

The user can save a voice from MP3 or WAV; import a folder of independent TXT/EPUB books; retain Polish characters and English punctuation; inspect encoding/language uncertainty; choose separate Polish and English model defaults; override an individual book's model and voice/cast; audition those choices; and produce complete, verified-as-labelled audiobooks with interruption recovery.

Completion requires real narration evidence for both languages and at least one supported alternative model path, not just a model dropdown or a passing text-only suite. The best-performing language presets may use the same engine or different engines. The application should support either result.

## Evidence and validation status

- During the preceding assessment on the same code baseline, `just check` passed: five schema exports, Pyright in all four environments, and **396 tests** (151 bookbinder, 21 narrator, 23 transcriber, 201 Studio). Two Studio dependency deprecation warnings were reported. That suite was not rerun for this documentation rewrite.
- The preceding review used twelve temporary diagnostics to confirm implementation defects, including content loss, stale output reuse, incomplete assembly, upload/locking problems, and incorrect checkpoint sharing. This rewrite retains the relevant findings; they have not been fixed.
- This revision rechecked the decoding, normalization, language propagation, XTTS coupling, and installed Polish/English sentence-splitter support. It checked the model shortlist and detector capabilities against linked primary sources on 2026-09-08.
- No candidate model was installed, downloaded, synthesized, or benchmarked during this rewrite. No listening winner, speed guarantee, or GPU compatibility result is claimed. Those are deliverables of slice B.
- Only this assessment document was rewritten. Application behavior and the original improvements roadmap were not changed.
