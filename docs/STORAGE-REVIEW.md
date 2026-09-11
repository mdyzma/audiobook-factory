# Review of the uncommitted catalog work

Reviewed 2026-09-11 against the working tree on top of `14767fd`.
About 1,200 lines of new Studio code, 18 tracked files changed, nothing committed.

## Verdict

The direction is defensible and the execution is careful. Four things should be
settled before this is committed: WAL, double storage, no way to forget a run,
and coverage on the module that touches irreplaceable audio.

## What is sound

- **The isolation constraint holds.** No pipeline environment imports the
  database. `grep` for sqlite/catalog across bookbinder, narrator and
  transcriber returns nothing. Stages still read and write files, inside a
  private data root the catalog materialises per run.
- **The suite is green**: 489 bookbinder, 51 narrator, 33 transcriber, 394
  studio, with schemas and types clean.
- **The database is intact**: integrity ok, no foreign-key errors, schema
  version 1. Immutability triggers on ten versioned tables, and migration
  refuses to run while a job is alive.
- **The one bookbinder change is a real fix.** `stale_fragments` omitted
  `cast_settings` from the expected fingerprint, so a book with per-role
  settings would have had every fragment reported stale.

## Findings

### 1. Nothing had been through the new path on real data

Before this review, every run in `data/audiobook.db` had status `legacy`, and
`import_batches`, `import_items`, `items`, `job_attempts` and `run_voices` were
all empty. The evidence for the new pipeline was unit tests plus a one-way
archive of what already existed.

I ran a book through it: import, chunk, dry run, assemble. It works, and the
catalog recorded the run correctly (`done`, fragments, export, reports, voices,
job attempts). That verification now exists; it did not before.

A probe run `82dbbbe6…` for book `zz-cat-probe` is still in the catalog. There
is no command to remove it (see finding 4).

### 2. WAL is off, and the code contradicts its own recommendation — FIXED

`docs/STORAGE-RECOMMENDATION.md` says to **retain WAL** and use a runtime
carrying the WAL-reset fix. `database.py` does the opposite: `wal_safe()`
returns true only for `>= 3.51.3`, `[3.50.7, 3.51.0)` and `[3.44.6, 3.45.0)`,
so every common release falls back to `DELETE` journaling. This machine runs
3.46.0, and the live database reports `journal_mode: delete`.

The effect is the concurrency property the queue was built around. Its own
docstring said readers must not block the writer so that listing the queue
never waits on a claim. Under rollback journaling they block each other, with a
five-second busy timeout behind it. `synchronous=EXTRA` is set on every
connection, including read-only page loads, where the recommendation said
`FULL` for valuable writes.

The fallback is silent. Nothing tells the user which mode they are in.

**Do:** restore WAL and warn on an unfixed runtime, or keep the fallback and
say so loudly at startup and in `just catalog-check`. Either way, document the
version windows; as written they are unexplained and look mis-specified.

### 3. Every fragment is stored twice, the chunk plan three times — FIXED

Run roots hold copies, not links: separate inodes, link count 1. The classic
`data/book/<slug>/` tree is still written, so the plan lives in the classic
tree, the run root and the `chunks` table.

| Directory | Size |
|---|---|
| `data/audio` (classic) | 15 MB |
| `data/book` (classic) | 2.6 MB |
| `data/assets` | 18 MB |
| `data/runs` | 23 MB |
| `data/audiobook.db` | 8.9 MB |

Four books, mostly unrendered, went from about 18 MB to about 65 MB. A
twenty-hour book is roughly 3.5 GB of fragments, so one render costs about 7 GB
under this layout, and comparing two voices doubles it. Comparing models across
two languages, which is the stated product goal, multiplies it again.

**Do:** hard-link run roots from the asset store, or stop copying into the
asset store and let the run root be the only home.

### 4. The catalog can only grow — FIXED

`catalog_cli` offers migrate, reconcile, check, books, runs, import-file,
import-folder, prepare, stage, process, backup, voice, output, progress and
restore. There is no way to forget a run, delete a book, or reclaim asset
storage. For a tool whose purpose includes re-rendering a book to compare
voices and models, disk use is monotonic with no remedy short of hand-editing
a database with foreign keys and immutability triggers.

**Do:** add `catalog-forget <run>` and an asset sweep before this is used in
anger.

### 5. Coverage is thinnest where the risk is highest — FIXED

79% across the new modules, but not evenly:

| Module | Coverage | What it does |
|---|---|---|
| `legacy.py` | 23% | Archives existing renders, which cannot be reproduced |
| `catalog_cli.py` | 53% | Every `just` recipe now calls it |
| `runs.py` | 78% | Orchestrates every stage |
| `database.py` | 89% | Includes the one-way queue migration |

`legacy.py` is the one to fix: it is the only code that touches hours of audio
nobody can render again.

### 6. Studio is now on the critical path for every stage — DOCUMENTED

`just synth` no longer runs the narrator; it runs Studio, which orchestrates.
Dependency isolation is preserved, but Studio is now a single point of failure
for the terminal workflow as well as the dashboard. `docs/HANDOFF-GPU.md` is
written for someone picking this up on the CUDA box and should say so.

### 7. Smaller

- The dry-run message still says `data/audio/<slug>/ now holds silence` when
  the file is under `data/runs/<id>/data/audio/<slug>/`.
- `data/.studio/queue.db` is left behind after migration, and deleting
  `data/audiobook.db` resurrects the migration refusal rather than a rebuild.
- `just verify` now always passes `--sample`, where it used to omit the flag at
  zero. Worth confirming the transcriber reads those the same way.

## What was fixed

**WAL.** `wal_safe()` became `wal_fix_present()` and now reports instead of
deciding. The catalog uses WAL unconditionally, reconciled on open so a
database left in rollback mode by the earlier build is corrected rather than
carried forward. An unfixed runtime is named in `just catalog-check` and
carried as a `warning` field rather than silently changing behaviour. The live
database is back in WAL, integrity clean.

**Double storage.** Run outputs are now folded into the stored asset: one inode
under two names. Verified on a real run, where every byte of fragment
duplication went away.

| | Apparent | On disk |
|---|---|---|
| A run made after the fix, with its assets | 33.9 MB | 25.3 MB |

Two preconditions had to land first, because a hard link can be written
through. The narrator wrote fragments straight to their final name, and the dry
run let ffmpeg truncate its target with `-y`; both now publish by rename, which
replaces the directory entry and leaves the shared inode alone. That also
removes the half-written wav the resume check used to detect by reading it back.

Linking is opt-in, not blanket. The first attempt collapsed everything and a
test caught it immediately: editing a voice reference rewrote the asset whose
name is the hash of what it used to contain. Only run outputs the pipeline
publishes by rename are folded; voice references and imported sources keep
their own bytes.

**Taking work back out.** `just catalog-forget <run>` removes a run, its
working directory and the bytes only it was holding, and works whether or not
the directory is still there. `just catalog-forget-book <slug>` removes a book
and every run of it, files included, so a reconcile cannot put it back.
`just catalog-sweep` deletes stored bytes nothing points at.

Assets are swept rather than deleted outright, because two runs of one book
share every fragment whose inputs did not change. A test pins that: forgetting
the first of two runs leaves the second's audio intact. Making the sweep
careless enough to ignore fragment references fails it, and the database's own
foreign keys catch it as well.

A stage on a run whose directory was deleted now says what happened and which
command fixes it, instead of failing on a missing lock file.

**Collapsing what was already duplicated.** `just catalog-collapse` folds older
runs' audio into the assets it duplicates. Run on this library:

| | Apparent | On disk |
|---|---|---|
| Before | 48.5 MB | 39.9 MB |
| After | 48.5 MB | 24.1 MB |

Integrity clean afterwards, still WAL. The probe book this review created was
then removed with `catalog-forget-book`, leaving the original four books and
their three legacy runs.

**Archival.** `legacy.py` went from 23% to 100%. The tests build a genuine
pre-catalog library rather than a hand-written one: a book is imported,
chunked, rendered and assembled through the pipeline, the results are moved to
where a pre-catalog installation kept them, and the catalog's record of the run
is dropped. What is left is what `just catalog-migrate` finds on somebody's
machine. Writing them turned up that archival only works after the book has
been registered, which is why `reconcile` does the two in that order.

They pin the parts that matter for audio nobody can render again: the fragments
and the finished file come across, the run says outright that its provenance is
uncertain rather than inventing a model and voice, archiving twice does not make
two runs, audio that no longer matches the text is refused, and a refusal leaves
every original file where it was. Archival also links rather than copies now,
like every other path into a run root.

**The handoff.** `docs/HANDOFF-GPU.md` has a section saying Studio is on the
path for every stage, that `just setup-studio` is therefore not optional, and
that output lives under `data/runs/<id>/` rather than `data/audio/<slug>/`. It
also says to carry `data/audiobook.db` and `data/assets/` together, and to
prefer `just catalog-backup` over copying a live database.

**The misleading path.** Both the dry-run notice and the assembly warning now
name the directory they actually read, absolutely, matching the lines around
them. A test pins it, and putting the hard-coded path back fails it.

## Nothing open from this review

The remaining work is on the roadmap rather than here: slice E, and the
comparison of synthesis models for Polish and English that the storage work was
always meant to serve rather than delay.

## A note on what removing a run directory does

While measuring, I deleted a run directory by hand, which is exactly what
someone would do to reclaim space. The catalog kept the run row, every stage
for that book then failed on a missing `.execution.lock`, and there was no
command to forget the run. `just catalog-reconcile` did not clear it;
recovering meant preparing a second run and leaving the first dangling.

That is finding 4 demonstrated rather than predicted, and it raises its
priority: the catalog is not robust against a missing run root, and the only
remedy is a command that does not exist yet.
