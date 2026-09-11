# Library storage and recovery

Implemented 2026-09-11. The catalog is local SQLite; no database server is needed.
It stores the library history and work queue. Keep it together with its assets.

## What is stored where

| Location | Contents |
|---|---|
| `data/audiobook.db` | Books, original-source revisions, prepared text versions, encoding and language decisions, chapters, chunk plans and chunks, model snapshots, voice revisions and references, audiobook runs and casts, completed fragments, exports, quality reports, imports and job attempts. |
| `data/assets/<hash-prefix>/<sha256>.<suffix>` | Preserved source bytes, reference audio, voice artifacts, rendered fragments and exported audiobooks. Identical bytes share one asset. |
| `data/runs/<run-id>/` | A narration's frozen configuration, text, voice files and execution manifests; stage outputs remain inside this run. These files can be reconstructed from a catalog backup. |
| `data/book/<slug>/`, `data/voices/` | Current authoring files and compatibility exports for existing tools. Editing these creates new versions when registered; it does not rewrite earlier runs. |
| `data/.studio/` | Process records, logs and locks. The old queue database is retained during migration, but the active queue moves into `data/audiobook.db`. |

A book can have successive text versions and several independent audiobook runs.
Every new run freezes its source text, model selection/settings and voice reference
revisions before it enters the queue. Chunking attaches one immutable plan to that
run. Changing the language, pronunciation, model or voice for a new narration
requires preparing another run. Per-role speed controls participate in fragment
fingerprints, so assembly detects audio rendered with different settings.

TXT and EPUB folder imports preserve the original bytes before parsing. Import
failures and review decisions remain in the catalog and are visible in Batch's
recent imports. Reimport with an explicit language/encoding to correct a decision.
Each file remains a separate book. Unsupported formats are recorded as such.

## Existing libraries

Stop Studio, queue workers and running stages, then run:

```bash
just catalog-migrate
just catalog-check
```

The legacy queue is copied with SQLite's backup API and preserved as
`data/.studio/queue-before-catalog.db`. Registration is idempotent. Existing source
and audio files stay in place. Historical audio is archived as a `legacy` run;
missing source bytes or unknown historical voice/model provenance are recorded
without substituting today's defaults. Legacy runs are available for download;
prepare a new run to synthesize a new narration.

If older scripts change the authoring files directly, run `just catalog-reconcile`.
Read its `errors` list: an incomplete or inconsistent legacy book may need repair.
Direct module commands remain file-based; use the supported `just` recipes for
catalog-aware execution.

## Choosing and resuming a narration

The ordinary `just chunk`, `synth`, `dryrun`, `assemble` and `verify` commands now
select an audiobook run. `chunk` resolves the configured preset for the book's
language. Later stages resume the latest compatible run. Global defaults do not
change an already prepared run.

For an explicit independent narration:

```bash
just catalog-prepare my-book my-voice my-model-preset
just catalog-runs my-book
```

Use the printed run ID with `just catalog-stage RUN_ID chunk`, then `synth`,
`assemble m4b`, and optionally `verify`. The model preset must exist in
`config/models.toml` and its environment must be installed. Separate Polish and
English defaults are supported; this storage change does not add or rank models.

Studio's book page lists run history and download links. Interrupted queue attempts
are reconciled using database attempts and process records. Retry a failed queue
step to resume valid completed fragments. A restored backup resets in-flight queue
items to pending; resume with Studio or `just drain`. It does not resurrect old
process IDs. The run lock prevents two stages writing the same run concurrently.

## Backup and restore

```bash
just catalog-backup /path/to/new-backup
just catalog-restore /path/to/new-backup /path/to/new-library
```

Both destinations must be new directories. Backup takes a consistent SQL snapshot,
copies all registered assets and checks their hashes. Restore checks database
integrity, foreign keys and asset hashes before publishing the restored directory.
It rebuilds book manifests, voice references, run inputs, registered fragments,
exports and reports. It refuses to overwrite an existing library.

To inspect or run a restored library with this checkout:

```bash
AUDIOBOOK_FACTORY_ROOT=/path/to/new-library just catalog-check
AF_NO_DRAIN=1 AUDIOBOOK_FACTORY_ROOT=/path/to/new-library just ui
```

Backups include committed catalog state, registered assets and configuration. They
do not include unimported source folders, process logs, unregistered work in
progress, virtual environments, or externally cached model weights. Partial audio
is registered periodically during synthesis; the most recent fragment may still
be only in the working directory. Stop stages before backup when you need all
just-finished work included. Keep training projects separately if you need to
repeat training. Store a copy on another drive; a backup beside the library does
not protect against loss of that drive.

Do not delete `audiobook.db` as a cache. Before destructive experiments, take a
backup and restore it into a separate directory. Assets intentionally accumulate
across revisions; automatic deletion/garbage collection is not implemented.

## Runtime and validation

The pinned interpreter currently contains SQLite 3.46.0. This installation uses
DELETE journaling with synchronous EXTRA. WAL is allowed only on SQLite releases
with the WAL-reset fix. `just catalog-check` reports the actual mode and integrity.
Database transactions stay short; rendering and asset copying happen outside them.
PostgreSQL remains a later option if multiple machines need a shared writable
catalog. No ORM or server was introduced.

The local migration preserved four books, 23 chapters, 3,717 planned chunks, one
voice, three historical audiobook runs, 35 rendered fragments, two exports and
three quality reports. A backup at `data/backups/catalog-2026-09-11` contains 54
registered assets. Restoration into a separate temporary library retained all
record counts and passed integrity and foreign-key checks. Repeating registration
created no duplicate versions.

Tests include an actual import → chunk → silent render → assemble → backup →
restore → assemble cycle and a real queue-launched chunk job. No synthesis model
weights were loaded for this validation. Polish/English pronunciation and cloned
voice quality still require listening tests with the chosen engines.
