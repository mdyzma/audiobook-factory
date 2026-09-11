# Where state lives, and whether it should live in a database

Written 2026-09-10, against the tree at `14767fd`.

The question: books, fragments, render metadata and the rest currently live as
files under `data/`. Should they move into SQLite or Postgres?

**Short answer.** Keep the files. Add one derived SQLite index that Studio owns
and can throw away. Do not introduce Postgres. There is a cheaper fix that
should land first, and it is not a database at all.

---

## 1. What is stored today

| Path | What it is | Written by | Shape |
|---|---|---|---|
| `data/sources/<slug>/` | The imported file, copied verbatim | ingest | opaque bytes |
| `data/book/<slug>/chapters.json` | Import record: title, encoding decision, language decision, chapters | ingest | one document |
| `data/book/<slug>/book.json` | `BookMeta`: resolved model, cast, chapter index, counts | chunk | one document |
| `data/book/<slug>/chunks.jsonl` | One fragment per line, the synthesis plan | chunk | append-ordered list |
| `data/book/<slug>/pronunciation.yml` | Per-book say-it-like-this | a person | one document |
| `data/audio/<slug>/<id>.wav` | Fragment audio | narrator | opaque bytes |
| `data/audio/<slug>/rendered.jsonl` | What was rendered, with real durations | narrator | append-only log |
| `data/audio/<slug>/fingerprints.jsonl` | What each fragment was rendered from | narrator | append-only log |
| `data/audio/<slug>/progress.json`, `report.json` | Live progress, and the verdict on a run | narrator | one document |
| `data/audio/<slug>/qa_report.json` | Transcription check, with per-fragment findings | transcriber | one document, large |
| `data/out/<slug>.m4b` | The deliverable | assemble | opaque bytes |
| `data/voices/<name>.json`, `data/voices/<name>/` | Voice profile and cached conditioning | clone | document plus tensors |
| `data/.studio/jobs/*.json`, `locks/` | Running jobs, and what each holds | studio | small documents |
| `data/.studio/queue.db` | The work waiting to happen | studio | **already SQLite** |

Two properties are load-bearing and easy to lose sight of.

**The environments cannot import each other.** `whisperx` and `tts==0.22.0`
cannot share a virtualenv, which is why there are three. Files under `data/`
are the entire interface between them, and `bookbinder/manifest.py` is the
contract, exported to `docs/schemas/` and mirrored by hand where it must be.

**Publication is already atomic.** `publish()` writes beside the target and
renames, so a reader sees the previous file or the complete new one, never a
half-written one. Resume works off append-only logs, where a run killed
mid-write loses at most a trailing line that the reader skips.

---

## 2. What actually hurts

I measured rather than guessed. Listing books on the dashboard, today, with
four books on disk:

| | |
|---|---|
| Books | 4 |
| Files read | 18 |
| Bytes parsed | 574 KB |
| Time | 20 ms |

Of those 574 KB, **549 KB are QA reports**, and no list view displays anything
from them. `get_book` loads the transcription check for every book so that a
page can print one word about render state. Extrapolated at the same shape:

| Books | Parsed per page load | Time |
|---|---|---|
| 50 | 7 MB | 0.3 s |
| 500 | 70 MB | 2.5 s |
| 2000 | 280 MB | 10 s |

That is the real problem, and it is worth being precise about its cause: it is
not that the data is in files. It is that a list reads whole documents to
answer a question about one field of each. A database would hide that mistake
rather than fix it.

The second problem is narrower. `Manifest.write()` publishes `chunks.jsonl` and
then `book.json`. Each is atomic alone, the pair is not, so a crash between the
two leaves a new fragment list beside a `book.json` whose `chunk_count`
describes the old one. The ordering is the safer of the two, but the window is
real.

Nothing else currently hurts. Four books is not a scale problem, concurrent
writers are already handled by the lock files and the queue's claims, and the
per-book files are read by exactly the stage that needs them.

---

## 3. Why not Postgres

Not at this stage, and probably not ever for this tool.

- **It adds an operator.** A server to install, run, back up, upgrade and
  connect to, for software whose entire promise is that a person who does not
  use a terminal can run it from `install.sh`. `docs/MANUAL.md` currently has no
  step that could fail because a service was not running.
- **It adds a driver to three environments.** `psycopg` would go into
  transcriber, bookbinder and narrator. The narrator's pins are documented as
  load-bearing to the point that `numpy` moving to 2.x breaks XTTS at inference.
  Adding a dependency there to solve a problem the tool does not have is the
  wrong trade.
- **It buys nothing here.** Postgres earns its keep with concurrent writers
  across machines, real access control, and queries that need a planner. This is
  one user, one machine, one worker at a time by design, and the biggest table
  in sight is a few thousand rows.
- **The audio cannot go in it anyway.** 15 MB today, gigabytes per shelf. The
  filesystem stays the store for everything that matters by volume, so a
  database would be a second place where truth lives, not a single one.

The honest threshold: Postgres becomes the right answer when more than one
machine writes the same data, or when this stops being a single-user tool.
Neither is on the roadmap.

---

## 4. Why not "move everything into SQLite" either

SQLite is genuinely free here: `sqlite3` is in the standard library, and all
four environments run the same Python 3.11.9 and the same SQLite 3.46.0. So the
dependency argument does not apply. The argument that does:

- **The contract would be duplicated.** The pydantic models in `manifest.py` are
  the interface between environments, exported as JSON Schema. Moving the
  contract into DDL means two definitions of the same thing, kept in step by
  hand, across environments that cannot import each other. That is precisely the
  failure mode `docs/DECISIONS.md` exists to prevent.
- **Resume would get worse, not better.** A twenty-hour render appends a line
  per fragment. Append-only files survive being killed at any instant, and the
  narrator already resumes off them. Replacing that with transactions on a file
  a second environment also opens is more machinery for the same guarantee.
- **Debugging gets harder.** When a book is narrated wrongly, the first thing
  anyone does is look at the fragment. `sed -n '412p' chunks.jsonl` beats
  opening a database, and it works from the CUDA box over ssh.
- **Migrations arrive.** `SCHEMA_VERSION` has moved 1 → 6 during this work.
  Files re-version by rewriting them; a schema change in a database is a
  migration to write, test and roll back.

---

## 5. What to do instead

### Now, and it is not a database

**Load the QA report only when something asks for it.** `get_book` should not
read `qa_report.json` to build a list. Making it lazy removes 96% of the bytes
from every list load, costs a handful of lines, and needs no new storage. Do
this before anything else, because it changes the scaling numbers above by more
than a database would.

**Publish the fragment plan and its metadata as one step.** Write both to
temporary names, then rename both, so a crash cannot leave a `chunk_count` that
disagrees with the file it counts. It is not perfectly atomic across two
renames, but it narrows the window from "an entire file write" to "one syscall".

Together these are perhaps half a day and they address everything measured.

### When the shelf passes about a hundred books

**Add a derived index in SQLite, owned by Studio, at
`data/.studio/library.db`.** One row per book with exactly the columns a list
needs: slug, title, author, language, encoding, model, reader, estimated hours,
state, fragment counts, whether it needs review, and the modification time of
each source file it was built from.

The rules that make this safe are the ones `queue.db` already follows:

- **Files remain the truth.** The index holds nothing that is not derived.
- **It is disposable.** Deleting it loses nothing; the next start rebuilds it.
- **Only Studio reads or writes it.** The pipeline environments never learn it
  exists, so the contract between them does not change and the isolation holds.
- **Staleness is detectable.** Each row records the mtimes it was built from, so
  a row whose files have moved on is refreshed rather than trusted.

That gives constant-time listing, ordering and filtering across a large library
without putting a database anywhere near the pipeline.

### Not planned

Fragment text, render logs, audio and voice profiles stay as files at every
size. They are read whole by one stage each, they are what a person inspects
when something sounds wrong, and none of them is queried across books.

---

## 6. Recommendation in one line

Fix the lazy load and the paired publish now; add a throwaway SQLite index for
listing when the library outgrows reading every file; leave the pipeline's
file contract alone; do not introduce Postgres.
