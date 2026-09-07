# audiobook-factory

Voice cloning plus ebook narration. Five stages across three deliberately
isolated Python environments.

## The one rule that matters

`whisperx` (pandas 2.x) and `tts==0.22.0` (pandas 1.x) can never share a
virtualenv. Do not "fix" a dependency error by merging environments or by
relaxing a pin. If numpy in `apps/narrator/` moves to 2.x, XTTS breaks at inference.
Background: `docs/DECISIONS.md`, extracted from `gemini-session.md`.

## Layout

| Directory | Environment | Stack |
|---|---|---|
| `apps/transcriber/` | A | WhisperX, numpy >=2.1, pandas >=2.2.3 |
| `apps/bookbinder/` | B | pure Python, ffmpeg, no torch |
| `apps/narrator/` | C | Coqui XTTS-v2, numpy <2, pandas <2 |

Environments communicate only through files under `data/`. The contract is
defined in `apps/bookbinder/src/bookbinder/manifest.py`; change it there and update
both consumers.

## Conventions

- Every command goes through `just`. Add a recipe rather than documenting a raw
  `uv run` invocation.
- uv owns the interpreter, the virtualenvs and the locks. There is no pyenv and
  no poetry. Python is pinned to 3.11.9 in all three environments.
- The narrator's pins live in `[tool.uv] constraint-dependencies`. Read the
  comments beside each one before changing it; every entry is load-bearing.
- `uv.lock` is committed. Regenerate with `just relock <env>`, never by hand.
- Tunables live in `config/pipeline.toml`, not in code.
- Stage 4 must stay resumable. A twenty-hour book cannot restart from zero.
- Audio is 24 kHz mono 16-bit PCM throughout.
- Run `just check` before committing: pyright and pytest in all three
  environments. Tests live in `<env>/tests/` and run against that environment's
  own dependencies.
- Tests that need model weights or CUDA do not belong in the suite. Use
  `just check-narrator` for XTTS loading and the CUDA box for fine-tuning.
- `docs/DECISIONS.md` records why the environments are split and which
  versions are pinned. Read it before changing any dependency.
- `docs/DEVELOPMENT.md` covers the day-to-day workflow: adding dependencies
  safely, what the tests do and do not cover, and the known failure modes.
- `docs/COMMANDS.md` is the reference for every `just` recipe. Update it when
  you add or change one.
- `docs/HANDOFF-GPU.md` is the brief for picking this up on the CUDA machine.
  Fine-tuning is the open task and has never run.
- `docs/RUNBOOK.md` holds task-oriented walkthroughs. Its terminal output is
  captured from real runs; if you change what a command prints, re-capture it
  rather than editing the block by hand.
- `gemini-session.md` is a raw transcript, kept locally and deliberately
  untracked. Everything load-bearing in it is already in `docs/DECISIONS.md`.

---

# context-mode — MANDATORY routing rules

You have context-mode MCP tools available. These rules are NOT optional — they protect your context window from flooding. A single unrouted command can dump 56 KB into context and waste the entire session.

## BLOCKED commands — do NOT attempt these

### curl / wget — BLOCKED
Any Bash command containing `curl` or `wget` is intercepted and replaced with an error message. Do NOT retry.
Instead use:
- `ctx_fetch_and_index(url, source)` to fetch and index web pages
- `ctx_execute(language: "javascript", code: "const r = await fetch(...)")` to run HTTP calls in sandbox

### Inline HTTP — BLOCKED
Any Bash command containing `fetch('http`, `requests.get(`, `requests.post(`, `http.get(`, or `http.request(` is intercepted and replaced with an error message. Do NOT retry with Bash.
Instead use:
- `ctx_execute(language, code)` to run HTTP calls in sandbox — only stdout enters context

### WebFetch — BLOCKED
WebFetch calls are denied entirely. The URL is extracted and you are told to use `ctx_fetch_and_index` instead.
Instead use:
- `ctx_fetch_and_index(url, source)` then `ctx_search(queries)` to query the indexed content

## REDIRECTED tools — use sandbox equivalents

### Bash (>20 lines output)
Bash is ONLY for: `git`, `mkdir`, `rm`, `mv`, `cd`, `ls`, `npm install`, `pip install`, and other short-output commands.
For everything else, use:
- `ctx_batch_execute(commands, queries)` — run multiple commands + search in ONE call
- `ctx_execute(language: "shell", code: "...")` — run in sandbox, only stdout enters context

### Read (for analysis)
If you are reading a file to **Edit** it → Read is correct (Edit needs content in context).
If you are reading to **analyze, explore, or summarize** → use `ctx_execute_file(path, language, code)` instead. Only your printed summary enters context. The raw file content stays in the sandbox.

### Grep (large results)
Grep results can flood context. Use `ctx_execute(language: "shell", code: "grep ...")` to run searches in sandbox. Only your printed summary enters context.

## Tool selection hierarchy

1. **GATHER**: `ctx_batch_execute(commands, queries)` — Primary tool. Runs all commands, auto-indexes output, returns search results. ONE call replaces 30+ individual calls.
2. **FOLLOW-UP**: `ctx_search(queries: ["q1", "q2", ...])` — Query indexed content. Pass ALL questions as array in ONE call.
3. **PROCESSING**: `ctx_execute(language, code)` | `ctx_execute_file(path, language, code)` — Sandbox execution. Only stdout enters context.
4. **WEB**: `ctx_fetch_and_index(url, source)` then `ctx_search(queries)` — Fetch, chunk, index, query. Raw HTML never enters context.
5. **INDEX**: `ctx_index(content, source)` — Store content in FTS5 knowledge base for later search.

## Subagent routing

When spawning subagents (Agent/Task tool), the routing block is automatically injected into their prompt. Bash-type subagents are upgraded to general-purpose so they have access to MCP tools. You do NOT need to manually instruct subagents about context-mode.

## Output constraints

- Keep responses under 500 words.
- Write artifacts (code, configs, PRDs) to FILES — never return them as inline text. Return only: file path + 1-line description.
- When indexing content, use descriptive source labels so others can `ctx_search(source: "label")` later.

## ctx commands

| Command | Action |
|---------|--------|
| `ctx stats` | Call the `ctx_stats` MCP tool and display the full output verbatim |
| `ctx doctor` | Call the `ctx_doctor` MCP tool, run the returned shell command, display as checklist |
| `ctx upgrade` | Call the `ctx_upgrade` MCP tool, run the returned shell command, display as checklist |
