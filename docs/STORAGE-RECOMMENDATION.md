# Storage recommendation: SQLite catalog with files for audio and sources

Assessed 2026-09-10 against `14767fd`, including changes since the earlier assessment baseline `a265f90`. This is a recommendation; no application storage or existing library data has been migrated.

## Recommendation

**Yes, a database is reasonable now. Use SQLite for the library catalog, book revisions, chunk plans, audiobook runs, and their relationships. Keep original books, voice recordings, generated audio, and model weights as files. Defer PostgreSQL.**

SQLite is already implemented for the persistent queue at `data/.studio/queue.db`. The next decision is how far to extend that approach. A gradual extension is justified by folder imports, resumable processing, and comparing different narrations of the same book. Replacing every JSON file in one change would spend too much effort away from the main product goal.

The target remains cloned voices from MP3/WAV, separate TXT/EPUB files imported from a folder, accurate text decoding, reliable Polish/English decisions, and independently chosen synthesis models for those languages. A database should preserve those decisions and their results. It cannot improve pronunciation, recover incorrectly decoded characters, or establish which model sounds best.

Recommended order:

| Priority | Investment | Value at this stage |
|---|---|---|
| Immediate | Update the SQLite runtime used by Studio; establish database migrations and backups | Protect the queue already implemented and prepare for valuable catalog data |
| Next | Stable book identity, immutable source/text revisions, persistent import review, and frozen run settings | Prevent changing inputs or defaults from changing the meaning of approved work |
| Next, incrementally | Catalog chapters/chunks, rendered fragments, exports, and voice revisions | Make retries, comparisons, progress, and provenance reliable across the library |
| Later | Rich search, detailed evaluation history, advanced reporting | Useful once there is enough real library activity to justify them |
| Defer | PostgreSQL, distributed workers, wholesale removal of manifests | Current needs do not justify the added operational and migration cost |

Continue Polish/English listening evaluations alongside this work. A complete storage redesign should not become a prerequisite for testing another synthesis model.

## What changed, and how data is stored today

The project has progressed beyond the original roadmap's description of unversioned, unprotected JSON storage. It now has manifest schema version 6, decoding and language provenance, model selection recorded during chunking, fragment fingerprints, stronger output publication, assembly checks, folder deduplication, a transactional queue, a draining worker, and a shared GPU gate.

The current architecture is nevertheless primarily a file pipeline:

| Information | Current location | Current behavior |
|---|---|---|
| Uploaded/raw inputs | `data/raw/` | Files used by book import and voice preparation |
| Imported book source | `data/sources/<slug>/<filename>` for new imports | Copied and hashed; downstream metadata points to the staged file |
| Extracted book text | `data/book/<slug>/chapters.json` | Metadata plus ordered chapters and paragraphs; includes source, encoding, and language decisions |
| Book and chunk plan | `data/book/<slug>/book.json`, `chunks.jsonl` | Current metadata, resolved model and cast, ordered spoken text, source references, and text substitutions |
| Rendered fragments | `data/audio/<slug>/<chunk_id>.wav` | Audio files; fingerprints determine whether fragments can be reused |
| Render bookkeeping | `rendered.jsonl`, `fingerprints.jsonl`, `progress.json`, `report.json` under that audio directory | Current fragment results, resumability records, progress, and render summary |
| Quality results | `data/audio/<slug>/qa_report.json` | Current verification report tied to a rendered fingerprint |
| Finished audiobooks | `data/out/<slug>.<format>`, `<slug>.chapters.json` | Outputs share the book slug; the same format is replaced on another assembly |
| Voice library | `data/voices/<voice>.json`, `data/voices/<voice>/` | Profile and references, cached `latents.pt`, audition audio, and clone metadata |
| Voice preparation/training | `data/processed/`, `data/datasets/<voice>/`, `training/` | Cleaned recordings, reference clips, CSV transcripts, and training artifacts |
| Queue | `data/.studio/queue.db` | SQLite `items` and `meta` tables; stages, arguments, order, claims, status, and a job ID |
| Running/completed processes | `data/.studio/jobs/`, `data/.studio/locks/` | Job JSON, logs, exit records, and filesystem locks, reconciled with process state |
| Defaults and authoring choices | `config/models.toml`, `config/pipeline.toml`, `config/cast.yml`, book-specific files | Version-controlled defaults and current overrides; some are copied into manifests |

The SQLite queue uses WAL mode, a five-second busy timeout, and short `BEGIN IMMEDIATE` write transactions. Claiming an item and changing its status is atomic. A book's stages are added together in one transaction. These are useful foundations to retain. See [queue implementation](/Users/michaldyzma/projects/audiobook-factory/apps/studio/src/studio/queue.py:191).

Studio still builds the library by scanning book and voice directories and reading their files. It combines book metadata, render reports, QA reports, and output existence into a view. There is no relational book catalog, audiobook edition entity, or complete revision history. See [library reads](/Users/michaldyzma/projects/audiobook-factory/apps/studio/src/studio/data.py:314).

The local data inspected for this assessment is small: four book directories, 3,717 declared chunks, two voice profiles, and a 28 KiB queue database. A copied queue snapshot contained zero queue items and schema version 1. All four existing `book.json` files still declare manifest version 1; two reference source files absent at their recorded locations. This is evidence that migration must accommodate older artifacts, not evidence of a database capacity problem. Existing samples do not demonstrate that all newly implemented stages have run on real books.

## Where a database would add real value

### 1. Preserve book identity and revisions

Folder import already distinguishes identical content, renamed duplicates, and changed content at a previously imported path. However, its persistent identity is still the directory slug. Import replaces `chapters.json`, and staging the same filename under the same slug replaces the previous staged source. There is no retained sequence of source or extracted-text revisions. See [folder classification](/Users/michaldyzma/projects/audiobook-factory/apps/bookbinder/src/bookbinder/library.py:128) and [source staging](/Users/michaldyzma/projects/audiobook-factory/apps/bookbinder/src/bookbinder/ingest.py:30).

A stable internal book ID should survive title, filename, and display-slug changes. A changed source should create a new revision, with the old revision still available to any audiobook that used it. Identical source bytes can share one stored asset without forcing every import, book edition, or narration to become the same record.

Import currently extracts from the external source before staging and hashing it. A file changed during that interval could produce text and a hash from different contents. Stage the bytes first, then extract and hash that same immutable asset. This is a code-inspection risk; it was not reproduced during this review.

### 2. Preserve exactly what was approved for narration

Recording a model and cast in `book.json` is a major improvement, but it is a snapshot of the current chunked book, not an immutable queued run. Queue entries contain a slug and arguments. The batch planner queues `chunk` without a resolved model/cast snapshot; synthesis primarily receives a voice creoverride. Defaults can therefore still matter when an imported book waits to be chunked, and replacing the current manifests changes the files that later stages address. See [batch submission](/Users/michaldyzma/projects/audiobook-factory/apps/studio/src/studio/batch.py:219).

Create a persistent run request when the user approves work. Record the source/text revision, confirmed language, selected model preset and concrete checkpoint revision when known, synthesis settings, voice revisions, and cast assignments. Resolve these before enqueueing; chunking must use that saved choice. Attach the completed immutable chunk plan before synthesis starts.

This lets one book have, for example, two narrations made with different Polish model candidates without overwriting either. English defaults remain a separate choice. An unrecorded checkpoint revision must remain explicitly unknown rather than being inferred from today's configuration.

### 3. Keep import exceptions in the library

An import requiring an encoding or language decision currently returns before writing the book's `chapters.json`. Folder processing reports the exception, but that unresolved result is not a durable catalog record that Studio can reliably reopen. See [import review return](/Users/michaldyzma/projects/audiobook-factory/apps/bookbinder/src/bookbinder/ingest.py:393).

Persist each scanned file's import result, including failures and review reasons. A batch should retain its selected files and their outcomes after restart. One ambiguous Polish TXT file can wait for an encoding override while the other books proceed.

### 4. Make relationships enforceable

Today, a matching slug/path connects a book to its chunks, renders, and reports. Foreign keys and uniqueness constraints would express those relationships directly: every chunk belongs to a particular plan, every rendered fragment belongs to a particular audiobook run, and every QA result names the audio it checked.

The current fingerprints should remain the compatibility test for reusable audio. A database status of `done` cannot replace checking the expected fingerprint and the actual artifact. Likewise, the existing atomic file publication and completeness checks remain valuable.

### 5. Improve queue submission and recovery without overstating transactions

Atomic claims are already implemented. Duplicate batch submission is a different concern: the planner checks whether a book is queued before calling the queue's write transaction. Concurrent submissions can both pass that earlier check. Enforce submission idempotency inside the transaction, scoped to the intended run, while still allowing deliberate alternative narrations.

The worker starts a subprocess through the file-backed job runner and subsequently attaches the job ID to the queue item. Database state, process creation, and file publication cannot be committed in one SQLite transaction. See [worker handoff](/Users/michaldyzma/projects/audiobook-factory/apps/studio/src/studio/worker.py:123).

Give each execution attempt a persistent ID before launch. Carry that ID into the job record and result files; reconcile interrupted handoffs before retrying. Preserve the existing book/GPU locks during migration. Design for repeatable recovery and duplicate detection, not a promise that a subprocess will execute exactly once.

## SQLite or PostgreSQL?

| Consideration | SQLite | PostgreSQL | Project implication |
|---|---|---|---|
| Installation and operation | Embedded database; no separate server | Database service to configure, maintain, and back up | SQLite fits the local application |
| Current workload | Short metadata writes around long inference jobs | More concurrency than currently needed | Audio synthesis remains the expensive operation |
| Concurrent activity | Multiple connections; writes serialize | MVCC plus row-level locking supports more concurrent writers | Dashboard reads and a few local workers fit SQLite |
| Several computers | Keep database access on its host, optionally behind an application API | Designed for clients connecting to a database service | Reconsider PostgreSQL for multiple independent worker hosts |
| Library size | Suitable for this metadata catalog | Also suitable | Book count alone is not a useful migration trigger |
| Operational cost | Small, but still needs migrations and backups | Additional deployment, access, upgrade, and recovery work | Spend that effort when deployment needs it |

SQLite's own guidance favors local storage with limited writer contention. This project's short bookkeeping operations and single-GPU gate fit that pattern; this is an architectural judgment, not a throughput benchmark. [SQLite usage guidance](https://www.sqlite.org/whentouse.html).

WAL allows concurrent reads and writes but still permits only one writer at a time. Keep the database on a local filesystem; do not share its live file between the Mac and a GPU PC over SMB/NFS. A browser accessing Studio remotely is different: database access can remain entirely on Studio's host. [SQLite WAL documentation](https://www.sqlite.org/wal.html).

Reconsider PostgreSQL when independent machines need concurrent database writes, measured write contention remains after shortening transactions, or a hosted multiuser service needs database operations such as managed failover. PostgreSQL's concurrency model and locking support those requirements, but it will not provide shared audio storage or GPU scheduling by itself. [PostgreSQL concurrency documentation](https://www.postgresql.org/docs/current/mvcc-intro.html).

## Recommended data model

Keep **source revision**, **prepared text**, **chunk plan**, and **audiobook run** distinct. Changing an encoding decision changes prepared text even when the source bytes stay the same. Changing a model's input limit can change chunk boundaries. Changing a voice can create another audiobook using the same text and chunk plan.

These are logical entities, not a requirement to implement every table in the first change:

| Entity | Important information and relationships |
|---|---|
| `books` | Stable ID, editable slug/title/author, current revision reference, archive state |
| `assets` | Content hash, relative storage key, kind, byte size; media duration/sample rate where applicable |
| `book_revisions` | Book ID, source asset ID, source filename/path provenance, creation time |
| `text_versions` | Book revision ID, extraction/decoder versions, encoding decision, language decision and override, normalization settings, review state |
| `chapters` | Text version ID, reading order, title, extracted paragraphs, EPUB/source references |
| `chunk_plans` | Text version ID, chunker version, model constraints, pronunciation/substitution rules snapshot, plan fingerprint |
| `chunks` | Plan ID, chapter ID, ordinal, source text, spoken text, substitutions/source spans, language, role, pause and estimate |
| `voices` / `voice_revisions` | Stable voice ID; immutable profile, reference asset IDs/hashes, reference languages, preparation settings, engine-specific cache identity |
| `audiobook_runs` | Book/text version and plan IDs, resolved model snapshot, voice/cast revision assignments, settings, status and timestamps |
| `render_fragments` | Run ID and chunk ID, expected input fingerprint, audio asset, duration, completion state, attempt/error information |
| `exports` | Run ID, audio asset, format, chapter metadata, assembly settings and fingerprint |
| `import_batches` / `import_items` | Selected folder/files and scan order; unchanged/duplicate/revised/review/failed outcomes, overrides, linked book revision |
| Queue items / job attempts | Run ID, stage, submission key, attempt ID, claim owner/token, process identity, timestamps, result and error |
| `qa_runs` / findings | Run ID, exact rendered fingerprint, verifier/model settings, chunk findings; detailed reports can initially remain files |

Use ordinary columns for fields used in filtering, joins, ordering, and constraints. Use validated JSON for structured provenance, backend-specific settings, substitutions, and initial cast snapshots. Avoid turning the whole catalog into a generic key/value table; also avoid normalizing every detector sample or synthesis setting into its own table.

Decoded chapter and chunk text can reasonably live in SQLite as `TEXT`. Original TXT/EPUB bytes must remain preserved as source assets. Storing correctly decoded Unicode in a database does not repair a wrong decode. Keep printed/source text separate from spoken substitutions so Polish diacritics, English punctuation, and corrections remain inspectable.

Require at least:

- Unique `(plan_id, ordinal)` and `(plan_id, chunk_id)` chunk identities; a chunk ID is not globally unique.
- Unique `(run_id, chunk_id)` current fragment results, with separate attempts where history is needed.
- Foreign keys between plans, text versions, chapters, runs, voices, and assets; reject cross-plan fragment assignments.
- Transactional idempotency for repeated submission of the same request, with an explicit way to create a new comparison run.
- Immutable settings once work starts; corrections produce new versions instead of mutating historical inputs.
- Expected chunk counts and fingerprint agreement before a run/export is marked complete; dry-run audio remains explicitly identified.

Reference recordings are the reusable voice asset. Model-specific latents and embeddings are derived caches. Preserve the reference files and their content hashes so a new backend can use the same voice library; do not make an XTTS cache the only durable representation of a voice.

## Files, manifests, and database ownership

Use one application database, with `data/audiobook.db` as the eventual path already suggested by ARCH-01. Reuse the queue implementation and migrate its existing records when the catalog is introduced. Avoid permanently separating a queue database and a catalog database that must agree about a run. The existing queue must not be renamed while workers have it open.

The database becomes authoritative for library identity, version relationships, saved choices, and run state. Source/audio/model bytes remain authoritative in their files. File assets should use immutable revision or content-based locations; retain relative storage keys rather than treating an original absolute import path as the asset's identity. Friendly slugs can remain in the UI and convenient export names.

Keep versioned JSON/JSONL manifests as the boundary between the isolated Python environments. They are useful for reproducibility, debugging, offline inspection, and keeping incompatible ML dependencies apart. After a domain moves into the catalog, its manifests are generated execution snapshots or registered stage results, not separately editable competing records.

Put SQL and migrations in a small Studio-owned persistence layer. A command-line entry point in that environment can register validated manifests without requiring a running web server. Have the supported `just` workflows invoke that registration path, including CLI-only import/chunk/render. Narrator and Transcriber can continue consuming manifests and producing results without database dependencies. During transition, provide an explicit reconciliation/import command for older standalone outputs.

A practical completion protocol is:

1. Record the intended run/attempt and immutable input references in a short transaction.
2. Perform extraction or synthesis outside the transaction.
3. Write the artifact to a temporary location, validate it, then publish it under its immutable final key.
4. In another short transaction, register the artifact and its completed fragment/stage result.
5. On restart, reconcile files published before step 4 and records whose files are missing. Match attempt IDs and fingerprints; do not infer success from a filename alone.

No database transaction covers an external WAV write. A crash between publication and registration should leave an identifiable recoverable orphan, not a record claiming a missing file is complete. Protect files referenced by active runs from cleanup. For power-loss guarantees, the publication/backup design also needs appropriate file and directory synchronization; atomic rename alone is not the whole durability contract.

## Reliability requirements before expanding SQLite

**Update the SQLite library loaded by Studio.** The inspected Studio environment uses Python 3.11.9 with SQLite 3.46.0. SQLite documents a rare WAL-reset corruption race affecting older releases when multiple connections write/checkpoint concurrently. The fix is in 3.51.3 and later, with documented backports in 3.44.6 and 3.50.7. Use a supported runtime carrying that fix and verify `sqlite3.sqlite_version` in Studio; updating a system SQLite executable does not establish what Python loads. No corruption was observed in the inspected snapshot. [SQLite WAL-reset fix](https://www.sqlite.org/wal.html#walresetbug).

Retain WAL and bounded busy handling. Enable foreign-key enforcement on every connection and set `synchronous=FULL` explicitly for valuable catalog writes. SQLite documents that `NORMAL` in WAL mode can lose recently committed transactions after power loss. Keep transactions short: never hold a write transaction for an entire audiobook, model load, or audio write. [SQLite foreign keys](https://www.sqlite.org/pragma.html#pragma_foreign_keys), [durability settings](https://www.sqlite.org/pragma.html#pragma_synchronous).

Use one ordered database migration history, separate from manifest format versions. The current queue stores a version but returns from initialization when the `items` table exists; that is not an upgrade system. Apply migrations once before workers start, validate the version on open, and refuse unknown newer schemas. Avoid a schema version per table and avoid schema changes in request handlers. See [current initialization](/Users/michaldyzma/projects/audiobook-factory/apps/studio/src/studio/queue.py:232).

Add a documented backup/restore command before treating the database as the only copy of user decisions. Use SQLite's backup API or another supported snapshot method for a live database, not an ordinary copy of its main file. Back up the immutable source/reference/audio assets identified by that snapshot as well, and prevent cleanup during backup. A database backup alone does not restore the audiobooks. [SQLite backup API](https://www.sqlite.org/backup.html).

Check database integrity and foreign keys, then restore into an empty data directory and verify asset hashes and a resumed job. Existing Git history does not back up this library: `data/` and `training/` artifacts are ignored. Keep raw sources, reference voices, and human decisions longer than reproducible caches; make deletion respect references from retained revisions and runs.

## Implementation sequence and acceptance criteria

| Step | Scope | Completion evidence |
|---|---|---|
| 0 — Harden the existing queue | Patched SQLite runtime, migration runner, backup/restore procedure | Runtime fix verified; queue ordering/claim tests still pass; backup restored successfully |
| 1 — Catalog imports | Books, assets, source/text revisions, persisted review items; import existing metadata | A renamed duplicate is recognized; a revised file retains the previous version; an ambiguous TXT remains reviewable after restart |
| 2 — Freeze execution inputs | Run requests, model/language/cast snapshots, chunk-plan identity; link queue to runs | Changing a default or reimporting a book after queueing cannot alter the approved run; duplicate submission is prevented transactionally |
| 3 — Catalog production results | Chapters/chunks, fragment records, attempts, exports and voice revisions; versioned result registration | Two model/voice renditions coexist; a failed chunk resumes correctly; QA stays attached to the audio it evaluated |
| 4 — Switch library reads | Studio catalog queries, generated manifests, legacy import/reconcile tooling | CLI and Studio show the same state; foreign keys/counts/artifact hashes agree; no ongoing dual writing of authoritative metadata |

Steps 1–3 span environment contracts and deserve separate reviewable changes. Start with the entities needed by each behavior, rather than building the entire proposed schema upfront.

During cutover, stop queue writers and the draining worker, settle or explicitly preserve outstanding attempts, take a recoverable backup, and migrate queue plus catalog data under one controlled application switch. Retain the old database as a backup, not an alternate active queue. Once catalog decisions live in the database, update the existing documentation that says deleting the queue database only loses future work.

Import existing manifests idempotently. Preserve their recorded schema and settings; missing hashes, absent sources, and unspecified model revisions must become explicit legacy/unknown states. Do not fill historical records from today's defaults. Verify artifacts before claiming they are resumable. If a source is missing, preserve available text/audio and mark the source unavailable rather than dropping the book. The four version-1 manifests present locally provide useful migration fixtures.

Rollback must restore a matched database/artifact snapshot before new writes resume. Simply pointing the application at the old JSON files after accepting new catalog edits would discard those edits. Run the first migration against a copy of the library and keep the original until import counts, content fingerprints, and restore behavior have been checked.

## Effect on the previous roadmap assessment

ARCH-01's SQLite direction is sound, but its original premise is stale: the code now has versioned schemas, atomic publication in several stages, fingerprints, and a transactional queue. Its suggested tables also need explicit revision/run relationships for this project's model comparisons. SQLite can technically store BLOBs; keeping audio outside it is the recommended design here, not a database limitation.

Revise the earlier “queue subset now, full migration later” position to **“queue hardening now; incremental catalog and immutable run records next; wholesale file replacement still deferred.”** This follows the project's new folder workflow and the user's request for a durable library. PostgreSQL remains conditional on a different deployment/concurrency need.

The highest-value outcome is being able to answer: **Which source bytes and decoding decision produced this text, which language/model/voice read each chunk, and which completed audiobook contains that result?** SQLite can make those relationships durable without displacing the Polish/English synthesis work.

## Evidence and limits

Reviewed import, decoding/language metadata, folder classification, manifest models, fingerprinting, synthesis/assembly/verification outputs, voice artifacts, Studio library reads, batch submission, queue transactions, worker handoff, and job persistence. Key contracts are defined in [manifest.py](/Users/michaldyzma/projects/audiobook-factory/apps/bookbinder/src/bookbinder/manifest.py:174), [chunk.py](/Users/michaldyzma/projects/audiobook-factory/apps/bookbinder/src/bookbinder/chunk.py:188), and [job storage](/Users/michaldyzma/projects/audiobook-factory/apps/studio/src/studio/jobs.py:245).

`just check` passed on 2026-09-10: five schema exports current; zero type-check errors; 940 tests passed across Bookbinder (489), Narrator (51), Transcriber (33), and Studio (367). Studio reported two dependency deprecation warnings. No real-model synthesis, migration implementation, database load benchmark, or power-loss test was performed.

The live queue could not be opened through the attempted read-only connection. Inspection therefore used a temporary copy while no WAL sidecar was present and the source file's size/modification time remained unchanged; the copied snapshot passed `quick_check`. This is limited snapshot evidence, not validation of a live backup procedure or a running queue.
