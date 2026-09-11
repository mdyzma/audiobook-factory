# Storage implementation plan

Started 2026-09-10. Completed 2026-09-11. Implements the models discussed after `STORAGE-RECOMMENDATION.md`.

## Intended behavior

Import separate TXT/EPUB files into a durable library; preserve source bytes,
encoding/language decisions and successive prepared texts. Save voices with
versioned references. Each audiobook run binds a chunk plan, model settings and
voice revisions, keeps its own outputs, and survives interruption. Studio and
supported CLI commands use the same catalog. Existing manifests remain the
execution contract between isolated environments.

## Execution checklist

- [x] Database foundation: ordered migrations, foreign keys, constrained models,
  shared queue storage, integrity checks and consistent backups.
- [x] Catalog: immutable assets, books/revisions/text/chapters/chunk plans/chunks,
  voices/references/model snapshots, runs/casts/fragments/exports/QA and attempts.
- [x] Import: idempotent legacy registration and folder imports with persistent
  review outcomes; source staging happens before extraction.
- [x] Execution: snapshot inputs before approval is queued; run-specific files,
  transactional submission deduplication and registered stage results.
- [x] Integration: Studio APIs/library, batch worker, CLI recipes, backup/restore
  commands and documented recovery.
- [x] Verification: migration, restart, duplicate submission, changed defaults,
  changed voices/text, separate narrations, failed stages and restore tests;
  full schema/type/test checks and a local dry-run demonstration.

## Implementation decisions

- Keep SQL in Studio, which already depends on the lightweight Bookbinder.
  Narrator/Transcriber keep their current environments and file contracts.
- Use the existing Python SQLite module. Enable WAL only on runtimes containing
  the documented WAL-reset fix; use rollback journaling with strong synchronous
  settings otherwise. Do not change the ML environment pins for a database fix.
- Keep one application database. Preserve the legacy queue during migration and
  refuse migration while recorded work is running.
- Copy source/reference bytes into immutable content-addressed assets. Record
  missing legacy provenance explicitly rather than guessing historical settings.
- Preserve current compatibility paths while execution uses isolated run roots.
  A new narration must not overwrite another run's audio.
- Keep ordinary command results visible and add narrow catalog/run commands;
  no ORM, database server, frontend rewrite, or new model download is required.

## Completion evidence

- `just check`: schema compatibility, type checks and 967 tests passed; an added
  restore-through-symlink regression also passed in the targeted storage suite
  (968 tests total across the final suites).
- Storage suite: 27 tests cover migrations, immutable versions, duplicate and
  concurrent submissions, encoding/language review, changed voice bytes and text,
  separate Polish/English model presets, partial audio, stale worker claims,
  recovery without job files, backup tampering and restoration.
- A real import/chunk/silent-render/assemble/backup/restore/reassemble cycle and
  queue-launched chunk process passed without loading ML weights.
- Local migration: 4 books, 23 chapters, 3,717 chunks, 1 voice, 3 historical runs,
  35 audio fragments, 2 exports, 3 QA reports and 54 assets; no registration errors.
- Backup: `data/backups/catalog-2026-09-11`. Restored into a separate library;
  all table counts matched, integrity was OK and foreign-key checks were empty.
  Repeated registration did not create duplicate records.
- Studio library, Batch, catalog APIs and all four book pages opened successfully
  against the restored library.
- Operating instructions: [STORAGE-OPERATIONS.md](STORAGE-OPERATIONS.md).
  Supported CLI recipes and the one-command launcher use run-specific outputs.

Real voice quality remains a separate Polish/English listening evaluation.
No model weights, ML dependency pins, manifest formats or PostgreSQL service were
changed. Backup covers registered state; unimported files, training workspaces,
raw job logs and external model caches need separate preservation. Legacy history
retains unknown provenance explicitly. Automatic asset garbage collection and
multi-machine scheduling are outside this implementation.
