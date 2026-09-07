# Roadmap: a front end

A plan for putting a graphical face on the pipeline, with effort estimates.
Nothing here is built. Written 2026-09-07.

## The constraint that shapes everything

A front end **cannot import this pipeline**. The three environments exist
precisely because they cannot share an interpreter: the narrator needs numpy 1.x
and the transcriber 2.x. No single process can hold both.

So the UI is an orchestrator, not a library consumer. It launches the same
commands `bin/audiobook` launches and reads the same files they write. That is a
constraint, but a mild one, because the stages already communicate entirely
through structured files:

| File | What the UI gets from it |
|---|---|
| `data/voices/<name>.json` | The voice list |
| `data/voices/<name>/audition.wav` | Something to play before committing hours |
| `data/book/<slug>/book.json` | Title, author, chapters, cast, estimated length |
| `data/book/<slug>/chunks.jsonl` | Every fragment with its text and role |
| `data/audio/<slug>/*.wav` | Progress, by counting them |
| `data/audio/<slug>/report.json` | Counts, failures, realtime factor |
| `data/audio/<slug>/qa_report.json` | Fragments whose audio disagrees with the text |

Four of those already have exported JSON Schemas in `docs/schemas/`, so the wire
format is defined before a line of UI exists.

## Recommendation

**A local web app, served by a small fourth environment, launched with
`just ui`.** Not Electron or Tauri to begin with.

The reasoning: packaging a desktop app means shipping a 3 GB multi-environment
Python backend inside an installer, which is the hardest part of the whole idea
and adds nothing until the interface itself is proven. A local web app skips it.
The browser is already installed, the backend is a process the user already runs,
and the same code becomes the inside of a desktop shell later if it earns one.

```
studio/                     a fourth uv environment; no torch, no ML
├── pyproject.toml          fastapi, uvicorn, and nothing heavy
└── src/studio/
    ├── app.py              routes
    ├── jobs.py             subprocess supervision
    └── static/             the interface
```

It stays a peer of the other three: its own lock, its own tests, its own pyright.

**Bind to localhost only.** This thing runs shell commands on behalf of whoever
can reach it. On `0.0.0.0` it is a remote code execution service. That is a
one-line decision worth making deliberately rather than by default.

## Phases

Ordered so something useful exists early, and each phase is independently worth
stopping at.

### Phase 0: emit progress — DONE (2026-09-07)

`synth` and `dryrun` now write `data/audio/<slug>/progress.json` as they go:
counts, percent, elapsed, ETA, current fragment and voice, last error, and the
writer's pid. Written atomically, because a UI will poll it while it is being
rewritten.

`RenderProgress` in `manifest.py` is the model, exported to
`docs/schemas/render_progress_v1.json`. `just progress <slug>` prints it and
`just watch <slug>` follows a render to completion.

Two details a UI must respect:

- **`running` is a claim, not a fact.** A killed process leaves it true forever.
  Compare `updated_at` against the clock; the CLI calls a render stale after two
  minutes of silence.
- **The ETA is rate-based and starts pessimistic**, because model loading takes
  about thirty seconds and lands on the first fragment. It settles quickly and
  is irrelevant on a book with thousands of fragments.

This also uncovered a real defect in the exported schemas: pydantic's default
schema mode omits computed fields, so `percent`, `eta_sec`, `realtime_factor`
and `ok` were missing from the published contract even though every writer
emits them. Schemas are now exported in serialization mode.

### Phase 1: read-only dashboard — DONE (2026-09-07)

`apps/studio/` is a fourth uv environment: fastapi, jinja2, no ML. It depends on
`bookbinder` as a path dependency, so the manifest models are shared rather than
mirrored a third time. `just ui` serves it on `http://127.0.0.1:8765`.

Pages: a dashboard listing books with state and progress and voices with their
assets; a book page with chapters, cast, render report, quality findings, the
finished audiobook and a player per fragment; a voice page with its audition.
A JSON API mirrors all of it, including a cheap `/progress` endpoint for polling.

Two things worth carrying into phase 2:

- **Names from URLs become file paths**, so they are matched against a strict
  pattern and rejected rather than sanitised. Six traversal attempts are covered
  by tests.
- **It reads `rendered.jsonl` in preference to `chunks.jsonl`.** Only the former
  carries audio paths, so reading the chunker's manifest alone means per-fragment
  playback silently never appears.

### Phase 2: run the pipeline — DONE (2026-09-07)

Buttons on the book and voice pages start stages; a live panel shows the
progress bar, fragment count and a streaming log, with a cancel button.

**It executes commands, so it never accepts one.** A request names an action
from a fixed table in `jobs.py` and supplies arguments validated before they
reach a process. Shell metacharacters, traversal and unknown containers are all
refused, with tests for each.

**Jobs outlive the page and the server.** Each runs in its own process group,
detached, writing to a log file, with its metadata on disk. Verified by killing
the server mid-render: the job carried on and completed, and the dashboard
picked it up again on restart.

**One render per book**, through a lock file naming the holding job. A lock held
by a process that no longer exists is cleared rather than blocking forever.

Three things worth knowing for phase 3:

- **A detached process cannot be waited on later**, so its exit code is written
  to a file by a small shell wrapper. Three states: exit file present means it
  finished with that code; no exit file and no process means it was killed, which
  is reported as `orphaned` rather than left running forever.
- **Finished children become zombies** and keep answering signal 0, so they were
  reported as alive. `reap()` clears them on every job listing; without it every
  completed render leaked a process entry for the life of the server.
- **The server's own virtualenv leaked into jobs**, making uv warn on every one
  that the active environment did not match the project. It is stripped now.

### Phase 3: authoring — DONE (2026-09-07)

A library page uploads voice samples and ebooks, records a sample in the browser
through MediaRecorder, and edits the cast as a form. The book page turns every
fragment's role into a dropdown.

**Corrections are keyed by `source_ref`, not chunk id.** Chunk ids encode
position, so re-chunking renumbers everything and would strand every correction.
`source_ref` names the paragraph in the source. They live in
`data/book/<slug>/role_overrides.json` and `bookbinder.chunk` applies them after
detection, so the heuristic becomes a starting point a person can overrule.
Tested by re-chunking twice and asserting the correction holds, and by clearing
one and asserting detection returns.

**Uploads never take a path from the caller.** The directory and extension are
chosen by the server; the filename contributes only a slugified stem. A traversing
name reduces to a bare stem inside `data/raw/`.

Worth noting for phase 4: writing `cast.yml` by hand rather than with a yaml
dumper keeps its explanatory comments, and one backup is kept at
`cast.yml.bak`.

### Phase 4: quality review — DONE (2026-09-07)

`/book/<slug>/quality` lists flagged fragments worst first: what was written,
what the transcriber heard, the audio, and a button to re-render just that one.

It needed a pipeline change, not only a page. `narrator.synth` gained `--only`,
which re-renders named fragments and **merges into `rendered.jsonl` rather than
replacing it**. Writing just the re-rendered fragments would have silently
discarded the rest of the book, which is the kind of defect that surfaces only at
assembly. `just resynth <slug> <ids>` exposes it.

Fragment ids reach a command line, so each is validated individually rather than
the list as a whole, and more than 200 at once is refused.

### Phase 5: desktop packaging — 3 to 5 days, optional

Wrap the local server in Tauri so it launches from an icon. Inherits every
packaging problem in [ROADMAP-DOCKER.md](ROADMAP-DOCKER.md), plus code signing on
both platforms.

Only worth it if the web UI proves people want to avoid the terminal entirely.

## Effort

| Phase | Days | Cumulative |
|---|---|---|
| 0. Progress signal | done | — |
| 1. Read-only dashboard | done | — |
| 2. Run the pipeline | done | — |
| 3. Authoring | done | — |
| 4. Quality review | done | — |
| 5. Desktop packaging | 3–5 | 19.5 |

**Roughly 15 to 20 focused days for all of it**, and about a week to phase 2,
which is the point where the terminal stops being necessary for ordinary use.

These are estimates for someone who knows the codebase. Treat the range as real:
phase 2 is where surprises live, because process supervision across three
environments on two operating systems is the kind of thing that looks finished
and then is not.

## What to do first

Phases 0 to 4 are done. Only phase 5, desktop packaging, remains, and it is the
one phase whose value is least certain: it buys an icon to click, at the cost of
inheriting every packaging problem in ROADMAP-DOCKER.md plus code signing.

Nothing needs it. The honest next step is to use the thing on a real book and
see what annoys you.

## The alternative worth considering

None of this is necessary. `bin/audiobook` already does the whole job in one
command, and an audiobook is not something anyone makes ten times a day. The
honest comparison is against improving the CLI: better progress output, a
`--watch` mode, nicer errors. That is a day of work rather than three weeks, and
it may be all this needs.

A UI earns its place if the role-correction screen from phase 3 turns out to
matter, because supervising the cast is genuinely awkward in a text editor and
genuinely pleasant in a browser. If that feature is not wanted, the case for the
rest is weak.
