# ROADMAP-IMPROVEMENTS.md

> **Audiobook Factory — Comprehensive Improvement Roadmap**
> Analysed from: `DEVELOPMENT.md`, `RUNBOOK.md`, `README.md`
> Audience: A team of 2–3 engineers turning this local-first tool into a production-viable audiobook factory.

---

## Executive Summary

Audiobook-factory is a functional voice-cloning and synthesis pipeline split across four isolated virtualenvs, driven by `just` recipes and a thin FastAPI dashboard. It works. It also has **no authentication**, **serves its entire data directory**, **shells out to ffmpeg with minimal input sanitisation**, and **silently produces broken audiobooks** in at least five documented failure modes — some of which it now detects after the fact, and some of which it still does not.

The 356 tests finish in three seconds, which is admirable, but they deliberately avoid the two things that actually break in production: model inference and audio fidelity. The contract between environments is a hand-mirrored JSON schema with a known risk of silent divergence. The dashboard starts long-running GPU jobs as the server process user with no sandboxing.

This roadmap addresses all of it. The first two phases are survival — closing security holes and building a test net that catches the known failure modes before users do. The later phases are growth — making the tool competitive with commercial audiobook production, not just faster than reading aloud.

---

## Section 1: Security Hardening

#### AUTH-01: Dashboard Authentication

**Rationale:** The docs state three times: *"It serves everything under `data/`, it starts processes, and there is no authentication."* Anyone on localhost — or on the LAN if the bind address is changed — can upload arbitrary files, start GPU renders, edit the cast, and read all voice samples and audiobooks.

**Proposed approach:** Implement session-based auth with HTTP-only cookies. On first launch, generate a random 32-byte token, print it to the terminal (like Jupyter), and require it on first access. Store a bcrypt hash in `data/.studio/auth.json`. Add a `--token` flag to `just ui` for scripted access. For LAN deployment, support `--auth required` which enforces login (username + password set via env vars or a `.env` file) and rejects unauthenticated requests with 401. Default remains token-on-localhost for the single-user local case.

**Effort:** M
**Priority:** P0
**Dependencies:** None
**Risks:** The current local-only workflow must remain zero-friction. Any auth that adds steps to `just ui` on a developer laptop will be circumvented or removed. The token-printed-to-terminal model avoids this.

---

#### AUTH-02: API Authorisation & Role-Based Access

**Rationale:** Even with authentication, every logged-in user can do everything — start renders, delete voices, overwrite casts. For multi-user deployments (a team sharing a GPU box), this is insufficient.

**Proposed approach:** Three roles: `viewer` (browse, listen, read reports), `operator` (start/cancel jobs, edit casts, upload), `admin` (manage users, delete data, change config). Enforce at the route level with FastAPI dependency injection. Store role assignments in `data/.studio/users.json`. Default single-user gets `admin`.

**Effort:** M
**Priority:** P1
**Dependencies:** AUTH-01
**Risks:** Over-engineering for the current single-user reality. Ship with all roles defaulting to admin and only expose the granularity when a second user is added.

---

#### PATH-01: Path Traversal Protection on File Serving

**Rationale:** The dashboard serves files from `data/` for playback and download. The docs do not mention any path sanitisation. A request to `/audio/../../etc/passwd` or `/audio/../../../.ssh/id_rsa` could escape the data directory.

**Proposed approach:** Centralise all file-serving through a single FastAPI dependency that resolves the path, calls `Path.resolve()`, and confirms the result starts with `data_dir.resolve()`. Reject any path that escapes. Apply this to every route that takes a filename or slug — uploads, downloads, audio playback, and the cast editor. Add a security test suite that probes with `../`, URL-encoded variants, symlinks, and null bytes.

**Effort:** S
**Priority:** P0
**Dependencies:** None
**Risks:** Symlinks inside `data/` pointing outward. Also resolve symlinks before the prefix check.

---

#### PATH-02: Upload Validation & Sandboxing

**Rationale:** The library page uploads voice samples and ebooks. An uploaded file is written directly into `data/raw/`. There is no validation beyond what ffmpeg does on read. A crafted EPUB or WAV could exploit a parser vulnerability.

**Proposed approach:** (1) Whitelist accepted MIME types and extensions — `.epub`, `.pdf`, `.txt`, `.wav`, `.mp3`, `.m4a` — and reject everything else at the route level. (2) Limit upload size (100 MB for books, 50 MB for voice samples — configurable). (3) Write uploads to a staging directory (`data/.studio/uploads/`), validate the file (attempt to parse the EPUB, probe the WAV header), then move to `data/raw/` only on success. (4) Run ffmpeg input probes on uploaded audio to confirm codec, sample rate, and duration are within bounds.

**Effort:** M
**Priority:** P0
**Dependencies:** None
**Risks:** EPUB is a ZIP format; a malicious ZIP bomb could exhaust disk. Add a decompressed size check before parsing.

---

#### CMD-01: Subprocess Input Sanitisation

**Rationale:** The pipeline shells out to ffmpeg for cleaning, silence generation, and mux. The bookbinder and scripts directories contain ffmpeg invocations. The docs never mention input sanitisation for these calls. While slugs and filenames are typically derived from user input (uploaded filenames), shell injection is a risk if any parameter reaches a shell command unquoted.

**Proposed approach:** Audit every `subprocess.run` / `subprocess.Popen` call across all four environments. Replace any `shell=True` with argument lists. Ensure filenames and slugs are passed as separate arguments, never interpolated into command strings. Add a validation function that rejects slugs containing anything other than `[a-z0-9_-]` and paths containing `..`, `\`, or null bytes. Apply it at every entry point — the CLI, the dashboard, and the justfile recipes.

**Effort:** S
**Priority:** P0
**Dependencies:** None
**Risks:** Some ffmpeg filter strings may require careful quoting. Use the list-of-args form throughout, which handles this automatically.

---

#### NET-01: Network Binding & CORS Hardening

**Rationale:** The dashboard binds to `127.0.0.1:8765` deliberately, but there is no mechanism to prevent a user from changing this for LAN access — at which point there is still no auth, no CORS policy, and no rate limiting.

**Proposed approach:** (1) Make the bind address configurable but default to localhost. If the user sets a non-localhost bind, print a warning and require `--auth required` (fail to start otherwise). (2) Set CORS to same-origin by default; allow explicit `--cors-origin` for embedding scenarios. (3) Add rate limiting via `slowapi`: 60 req/min per IP for API routes, 10 req/min for job-starting routes. (4) Set security headers: `X-Content-Type-Options: nosniff`, `Content-Security-Policy`, `Referrer-Policy: no-referrer`.

**Effort:** M
**Priority:** P1
**Dependencies:** AUTH-01
**Risks:** Rate limiting on localhost is mostly cosmetic but prevents runaway scripts. The real value is for LAN deployment.

---

#### SEC-01: Secrets & Sensitive Data Protection

**Rationale:** Voice samples are biometric data. Model weights represent significant compute investment. Training datasets may contain copyrighted audiobook excerpts. All of this lives unencrypted under `data/`, served by the dashboard with no access control.

**Proposed approach:** (1) Partition `data/` into public (finished audiobooks, audition clips intended for sharing) and private (raw voice samples, model weights, training data). The dashboard serves only the public partition for unauthenticated access. (2) Add optional at-rest encryption for the private partition using a file-level scheme (age encryption, or a FUSE encrypted directory). (3) Never include model weights or voice profiles in job log output. (4) Redact file paths in error messages shown in the dashboard.

**Effort:** L
**Priority:** P2
**Dependencies:** AUTH-01, PATH-01
**Risks:** Encryption at rest adds operational complexity for a local-first tool. Make it optional and off by default. The data partitioning is the high-value, low-cost part.

---

#### SEC-02: Model Provenance & Checkpoint Verification

**Rationale:** XTTS checkpoints are loaded with `torch.load` using an explicit allowlist (`allow_xtts_globals()`). This is good — the docs specifically warn against `weights_only=False`. But the model files themselves are downloaded on demand from the internet with no integrity check. A compromised model host could serve a malicious checkpoint that passes the allowlist but contains hostile code in tensor data.

**Proposed approach:** (1) Pin expected SHA-256 hashes for all downloaded model files in `config/pipeline.toml`. (2) On download, verify the hash before writing to cache. (3) On load, verify the hash again (or at least the file size as a fast check). (4) Document the hashes so users can verify independently. (5) Support a `--offline` mode that refuses downloads and fails if the cache doesn't contain the exact expected version.

**Effort:** M
**Priority:** P1
**Dependencies:** None
**Risks:** Hash mismatches on legitimate model updates. Version the hash alongside the model version string in config.

---

#### SEC-03: Dependency Supply-Chain Auditing

**Rationale:** The project has four separate `uv.lock` files with a combined 288 packages. The numpy/pandas/torch version conflicts are managed by hand. There is no automated vulnerability scanning. A compromised transitive dependency in any of the four environments could execute arbitrary code at import time.

**Proposed approach:** (1) Add `uv audit` (or `pip-audit`) to `just check` and CI. Run it across all four environments. (2) Add a weekly scheduled CI job that checks for new CVEs even if no code changed. (3) Pin all transitive dependencies in the lock files (already done via committed `uv.lock`) and add a PR template requiring lock file review for any dependency change. (4) Evaluate `uv`'s trusted publishing support for critical packages (torch, transformers, numpy).

**Effort:** S
**Priority:** P1
**Dependencies:** None
**Risks:** `pip-audit` may flag vulnerabilities in pinned old versions (transformers 4.40.2, numpy 1.26.4) that cannot be upgraded without breaking XTTS. Document these as accepted risks with the reason.

---

#### JOB-01: Job Isolation & Resource Limits

**Rationale:** Background jobs (synthesis, verification) run as the FastAPI server process user via `studio/jobs.py`. A job that exhausts GPU memory, fills the disk, or runs indefinitely affects the entire system. There is no memory limit, no CPU limit, no timeout beyond what the user applies by cancelling.

**Proposed approach:** (1) Run each job in a subprocess with explicit resource limits: `ulimit` for file size and core dumps, `timeout` as a safety net (default: 48 hours for synthesis, 2 hours for verification, configurable). (2) Monitor GPU memory usage from the parent process and kill jobs that exceed a configurable threshold. (3) Write job PIDs to `data/.studio/jobs/<id>.pid` and clean up orphaned PIDs on server start. (4) For Docker deployments, run jobs in separate containers with `--memory` and `--gpus` limits. (5) Never run jobs in the server's event loop — always a separate process.

**Effort:** M
**Priority:** P1
**Dependencies:** None
**Risks:** Process-based isolation is sufficient for single-machine use. Full container-per-job isolation (JOB-02) is a later improvement.

---

#### JOB-02: Container-Based Job Isolation

**Rationale:** On a shared GPU server, multiple users may queue renders simultaneously. Process-level isolation does not prevent one job from consuming all GPU memory or accessing another job's data directory.

**Proposed approach:** For the Docker deployment path, each render job launches as a short-lived container with: its own filesystem namespace (read-only except for its `data/audio/<slug>/` output mount), GPU resource limits via `--gpus` device indices, memory limits, and a network policy that prevents it from reaching the dashboard API. The dashboard orchestrates these containers via the Docker API. This replaces the current subprocess model only in the Docker deployment; localhost development continues using subprocesses.

**Effort:** XL
**Priority:** P3
**Dependencies:** JOB-01, DOCKER-01
**Risks:** Significant complexity. Only justified for multi-tenant GPU server deployments. Not needed for single-user local or single-user CUDA box.

---

## Section 2: Testing Strategy Overhaul

#### TEST-01: Tiered Test Strategy (Unit → Integration → E2E)

**Rationale:** The docs explicitly state: *"Most of the 356 tests are in bookbinder and studio, because that is where the logic that can silently corrupt a book sits"* and *"Nothing in the suite needs model weights or a GPU, so it finishes in seconds."* The test suite is fast but incomplete — it cannot detect XTTS truncation, voice quality regressions, or CUDA-specific failures.

**Proposed approach:** Three tiers with separate CI jobs:
- **Tier 1 (Unit, <5s):** Current tests. Pure logic, no models, no IO beyond temp directories. Runs on every push.
- **Tier 2 (Integration, ~2min):** Uses a small XTTS model (a few MB, stored in CI cache) or a mock TTS backend that produces real audio (tones at known durations). Tests the full file-based contract: ingest → chunk → synth → assemble → verify, with the mock backend. Runs on PR merge.
- **Tier 3 (E2E, ~30min):** Real XTTS-v2, real WhisperX, real ffmpeg. Synthesises a 50-fragment book, verifies it, checks WER. Runs nightly on a self-hosted GPU runner, or on merge to main via a GPU CI job.

Separate pytest markers: `@pytest.mark.unit`, `@pytest.mark.integration`, `@pytest.mark.e2e`. CI selects by marker.

**Effort:** L
**Priority:** P0
**Dependencies:** None
**Risks:** The "small XTTS model" may not exist — XTTS doesn't publish a toy checkpoint. Alternative: build a mock TTS engine that produces real WAV files with known content (e.g., DTMF tones encoding the fragment index) and verify by decoding the tones. This catches pipeline wiring errors without needing models.

---

#### TEST-02: Contract Testing Between Environments

**Rationale:** The docs warn: *"narrator and transcriber cannot import those models, since they resolve a different numpy. They mirror the shape by hand and write plain JSON. That is the one place a silent divergence can appear."* The existing `test_schemas.py` validates a copy of what each writer produces, but this is a snapshot test — it confirms the past, not the contract.

**Proposed approach:** (1) Generate JSON Schema from `manifest.py` on every CI run (already done by `just schemas`). (2) In the narrator and transcriber test suites, produce real output JSON (using their hand-mirrored models) and validate it against the generated schema. (3) Add a consumer-driven contract test: bookbinder reads the output of a narrator dry-run and a transcriber verify-run and asserts every field it expects is present and correctly typed. (4) On schema change, fail CI and require an explicit `SCHEMA_VERSION` bump with a migration note.

**Effort:** M
**Priority:** P0
**Dependencies:** None
**Risks:** The hand-mirroring is the fundamental problem. A longer-term fix (ARCH-05) would have the narrator and transcriber import the schema at runtime via a standalone package that carries no numpy dependency.

---

#### TEST-03: Property-Based Testing for Chunking & Roles

**Rationale:** Chunking and role assignment are pure logic functions that *"silently corrupt a book"* when they go wrong. The docs list specific failure modes: oversize chunks, missing chapter headings, roles mapped to voices that don't exist. These are exactly the bugs property-based testing excels at finding.

**Proposed approach:** Use Hypothesis to generate:
- Arbitrary chapter texts with varying lengths, dialogue markers, speaker labels, and special characters (em-dashes, Unicode quotation marks, Polish diacritics).
- Arbitrary cast configurations with missing voices, duplicate roles, and empty entries.
- Assert invariants: every chunk is under the per-language limit; every role resolves to a voice (or falls back to narrator); chapter boundaries are preserved; no text is lost (concatenated chunk text equals original chapter text); re-chunking with the same config produces the same fragments.

**Effort:** M
**Priority:** P0
**Dependencies:** None
**Risks:** Hypothesis can find edge cases that are technically violations but don't matter in practice (e.g., a 0-character chapter). Define the property tests over realistic text distributions, not fully arbitrary strings.

---

#### TEST-04: Audio Fidelity Testing

**Rationale:** *"XTTS can truncate a fragment, skip a clause or repeat a phrase, and none of that raises anything."* The current assembly tests use tones and check chapter timestamps, but they do not detect truncation, silence, or repetition in the audio signal itself.

**Proposed approach:** (1) **Silence detection:** after assembly, sample N fragments and compute RMS energy. Flag fragments below a threshold as potentially silent. (This partially exists in `bookbinder.assemble.all_silent` but only warns and only samples 12 fragments.) Make it a hard check in CI with a configurable threshold. (2) **Duration sanity:** for each fragment, the rendered audio duration should be within 20% of the estimated duration from chunking. Gross deviation indicates truncation or repetition. (3) **Truncation detection:** compare the end of the audio signal to the expected end (the last few words re-synthesised alone). If the tail doesn't match, flag it. (4) **Perceptual metrics:** for Tier 3 E2E tests, compute PESQ or SI-SNR between a reference synthesis and the current one to catch voice quality regressions. (5) Integrate all of these into `just verify` as optional `--strict` mode.

**Effort:** L
**Priority:** P1
**Dependencies:** TEST-01
**Risks:** Perceptual metrics require a reference signal. Store golden audio for a small test book and compare against it. The 20% duration tolerance may need tuning per language and voice.

---

#### TEST-05: Failure Mode Regression Tests

**Rationale:** The docs list at least eight specific failure modes, each with a detailed explanation of cause and symptom: numpy 2.x leak, silent audiobook from dry-run residue, stale progress, CUDA version mismatch, oversize chunks, `UnpicklingError` from torch 2.6, `BeamSearchScorer` import error, and speaker-latent errors. Each of these broke production once and was fixed, but there are no regression tests to prevent recurrence.

**Proposed approach:** Create a `tests/regression/` directory with one test per failure mode:
- `test_numpy2_leak.py`: Install a package that lifts numpy, run `just doctor`, assert narrator numpy is still 1.x. (Run in a subprocess with its own venv to avoid polluting the test environment.)
- `test_silent_audiobook_detection.py`: Place dry-run marker files alongside real audio, run assembly, assert the warning is emitted and `just verify` refuses.
- `test_stale_progress.py`: Write a progress file with an old timestamp, assert the system reports `STALE`.
- `test_oversize_chunk.py`: Feed text that exceeds the per-language limit, assert the chunker reports oversize and does not silently truncate.
- `test_torch_load_safety.py`: Attempt to load a checkpoint without `allow_xtts_globals()`, assert it fails rather than falling back to `weights_only=False`.
- Each test documents the original incident in a docstring with date and context.

**Effort:** M
**Priority:** P0
**Dependencies:** None
**Risks:** The numpy leak test is hard to automate without venv manipulation. May need to be a standalone script rather than a pytest case.

---

#### TEST-06: Security Test Suite

**Rationale:** None of the current tests verify that the dashboard rejects malicious inputs. With the security hardening in Section 1, each protection needs a test.

**Proposed approach:** Using `httpx` (already available via FastAPI's test client), write tests that:
- Attempt path traversal on every file-serving route (`../../../etc/passwd`, URL-encoded variants, double-encoded variants, symlink targets).
- Attempt file uploads with disallowed extensions (`.py`, `.sh`, `.exe`), oversized payloads, and MIME type spoofing.
- Attempt SQL injection / NoSQL injection in slug and cast parameters (even though there's no DB yet, parameter sanitisation should be agnostic to future storage).
- Attempt starting multiple concurrent jobs for the same book and assert the second is rejected.
- Verify that auth tokens are required when `--auth required` is set.
- Verify CORS headers are present and restrictive.

**Effort:** M
**Priority:** P0
**Dependencies:** AUTH-01, PATH-01, PATH-02
**Risks:** This suite will need to evolve as auth is implemented. Start with the path traversal and upload tests, which can be written against the current codebase immediately.

---

#### TEST-07: Performance & Concurrency Testing

**Rationale:** The dashboard serves concurrent WebSocket connections for job progress. Multiple books may be assembling simultaneously. The progress file is rewritten during a render and read by `just progress`. Under load, these concurrent reads and writes could corrupt state.

**Proposed approach:** (1) **Progress file concurrency:** start a render, then read `progress.json` in a tight loop from 10 concurrent readers. Assert no JSON decode errors and no stale values that violate monotonicity (fragment count should only increase). (2) **Dashboard load:** use `locust` or `wrk` to simulate 50 concurrent users browsing books, starting jobs, and streaming audio. Measure latency and error rate. (3) **Resumability under interruption:** start a render, kill the process at 50%, restart, assert no fragments are re-rendered and the final result is complete. (4) **Disk space:** run a dry-run on a book with thousands of chapters and assert the system handles it without OOM.

**Effort:** M
**Priority:** P2
**Dependencies:** None
**Risks:** Concurrency bugs are non-deterministic. Run each test 100 times and assert zero failures.

---

#### CI-01: CI Pipeline Modernisation

**Rationale:** Current CI runs `locks`, `justfile`, and a `check` matrix over four environments. It does not: run security audits, validate Docker images, run integration/E2E tests, check for stale model hashes, or generate coverage reports.

**Proposed approach:**

| Job | Trigger | What it does |
|---|---|---|
| `locks` | Every push | Current behaviour |
| `check` | Every push | Current matrix |
| `security` | Every push + weekly | `uv audit`, path traversal tests, dependency pin check |
| `schemas` | Every push | Current schema drift check + cross-environment contract validation |
| `docker-build` | Push to main | Build CPU images, run `just docker-smoke` |
| `coverage` | Push to main | `pytest --cov` in bookbinder and studio, upload to Codecov |
| `e2e-gpu` | Nightly + manual | Full pipeline on self-hosted GPU runner |
| `model-hash` | Weekly | Verify cached model hashes match pinned values |

Add a `workflow_dispatch` trigger for manual E2E runs.

**Effort:** M
**Priority:** P1
**Dependencies:** TEST-01, SEC-03
**Risks:** Self-hosted GPU runners cost money. Use a spot instance or a scheduled window on an existing GPU box.

---

#### TEST-08: Test Infrastructure & Fixtures

**Rationale:** Tests currently create ad-hoc temp directories and generate tones inline. There are no shared fixtures, no factory helpers, and no golden files. As the test suite grows, this leads to duplication and inconsistency.

**Proposed approach:** (1) **`conftest.py` per environment** with fixtures for: a temp `data/` tree, a sample EPUB (a 2-chapter book with known text), a sample voice profile JSON, a mock TTS backend, and a mock transcriber backend. (2) **Golden files** under `tests/golden/`: expected `chunks.jsonl` for the sample book, expected `report.json` for a dry run, expected `qa_report.json` for a known-bad fragment. (3) **Factory functions:** `make_chapter()`, `make_fragment()`, `make_cast()` with sensible defaults and keyword overrides. (4) **A `just test-golden` command** that regenerates golden files and diffs them — run manually when the contract changes intentionally.

**Effort:** M
**Priority:** P1
**Dependencies:** None
**Risks:** Golden files drift. Make regeneration explicit and diff-based, never automatic.

---

## Section 3: Architecture & Infrastructure Improvements

#### ARCH-01: Structured State Store

**Rationale:** State is currently scattered across `data/` as JSON files with implicit conventions: `progress.json` is rewritten during a render, `report.json` appears only at the end, `qa_report.json` is separate, job logs go in `data/.studio/jobs/`, and role overrides go in `data/book/<slug>/role_overrides.json`. There are no transactions, no migrations, and no schema versioning. Concurrent access risks corruption.

**Proposed approach:** Replace the JSON files with **SQLite** — a single `data/audiobook.db` file that requires no server. Tables: `books`, `voices`, `jobs`, `fragments`, `role_overrides`, `quality_findings`. Each table has a schema version; migrations run on app start. JSON files become views/exported snapshots for backward compatibility. Benefits: ACID transactions for job state changes, indexed queries for the dashboard (no more scanning directories), and a single source of truth. Keep `data/audio/<slug>/` as raw WAV storage (SQLite is not a blob store).

**Effort:** L
**Priority:** P1
**Dependencies:** None
**Risks:** SQLite concurrency limits: only one writer at a time. For this workload (one render at a time per book, dashboard reads), this is fine. Add WAL mode for concurrent reads during writes.

---

#### ARCH-02: Structured Logging & Observability

**Rationale:** The docs mention `report.json` and `progress.json` but there is no structured logging, no tracing, and no metrics. A render that fails at 3 AM on a remote GPU box produces a terminal transcript and a report file, but no way to reconstruct what happened at the fragment level. The dashboard shows live logs but they are unstructured subprocess output.

**Proposed approach:** (1) **Structured logging:** Replace all `print()` and `logging.info()` calls with `structlog` — JSON-formatted logs with trace IDs, fragment IDs, book slugs, and timing. (2) **Distributed tracing:** Add OpenTelemetry spans for each pipeline stage and each fragment synthesis. Export to Jaeger or an OTLP endpoint (configurable, default: no exporter for local use). (3) **Metrics:** Counters for fragments rendered, WER scores, render durations; gauges for GPU memory, queue depth. Export via Prometheus `/metrics` endpoint on the dashboard. (4) **Trace context propagation:** When a job is started from the dashboard, the trace ID flows to the subprocess via an environment variable and is included in all log output. The dashboard can filter logs by trace ID.

**Effort:** L
**Priority:** P1
**Dependencies:** ARCH-01
**Risks:** structlog adds a dependency to all four environments. It is pure-Python and numpy-free, so it's safe. OpenTelemetry is heavier; make it optional and off by default.

---

#### ARCH-03: Event-Driven Pipeline Communication

**Rationale:** The four environments communicate only through files under `data/`. The docs call this out as a deliberate design choice that enables the split, but it also means: no way to notify the dashboard when a stage completes (it polls), no way to chain stages without a justfile recipe, and no way to run stages on different machines without copying `data/`.

**Proposed approach:** Introduce a lightweight event bus using **Redis Streams** (or, for the local-only case, **filesystem watches with `watchfiles`**). Events: `BookIngested`, `BookChunked`, `FragmentRendered`, `RenderComplete`, `AssemblyComplete`, `VerificationComplete`, `JobFailed`. Each event carries the book slug, trace ID, and relevant payload. The dashboard subscribes and updates in real-time without polling. Stages publish events after writing their output files. For multi-machine deployment, Redis runs on the GPU box and stages subscribe from anywhere. For local development, the filesystem watch mode requires no additional infrastructure.

**Effort:** L
**Priority:** P2
**Dependencies:** ARCH-01, ARCH-02
**Risks:** Redis is an additional operational dependency. The filesystem watch fallback eliminates this for local use. Make Redis optional — the system must work without it, just with polling as today.

---

#### ARCH-04: Universal Resumability & Idempotency

**Rationale:** *"Stage 4 must stay resumable. A twenty-hour book cannot restart from zero."* Only synthesis (stage 4) is resumable. Ingestion, chunking, and assembly are not — if chunking is interrupted, it starts over. If assembly is interrupted mid-mux, the partial m4b is left behind.

**Proposed approach:** (1) Make every stage write a completion marker (`.stage_complete`) after successful finish, like the dry-run marker already exists. (2) On start, check for the marker and skip if present. Add `--force` to override. (3) For chunking: write fragments incrementally; on resume, skip fragments already in `chunks.jsonl`. (4) For assembly: ffmpeg mux is not resumable, but it is fast (minutes, not hours). Accept this and simply re-run it. (5) Track stage completion in the state store (ARCH-01) so the dashboard can show which stages are done without scanning for markers.

**Effort:** M
**Priority:** P1
**Dependencies:** ARCH-01
**Risks:** Incremental chunking requires a different file format (JSONL, not JSON) since JSON can't be appended to. The current `chunks.jsonl` is already JSONL — this is already partially in place.

---

#### ARCH-05: Shared Schema Package

**Rationale:** *"narrator and transcriber cannot import those models, since they resolve a different numpy."* They mirror the schema by hand, and silent divergence is the *"one place"* it can happen. This is the most fragile contract in the system.

**Proposed approach:** Extract `manifest.py` into a standalone package `audiobook-schemas` with zero dependencies — no pydantic (which may pull numpy), no pandas. Use plain dataclasses or TypedDict. Publish it as a wheel to a local directory or a private PyPI. All four environments depend on it. This eliminates hand-mirroring entirely. If pydantic is desired for validation, keep it in bookbinder only; the schema package defines the shape, bookbinder adds validation.

**Effort:** M
**Priority:** P1
**Dependencies:** None
**Risks:** This is a structural change that touches all four environments. Do it early in a phase before other changes complicate the schema.

---

#### ARCH-06: Error Taxonomy & Dead-Letter Handling

**Rationale:** *"A single bad fragment never kills a run, so this is the only place a partial failure shows up"* — meaning failures accumulate silently in `report.json` and are only visible after the render finishes. There is no retry mechanism and no way to distinguish transient failures (GPU OOM, recoverable) from permanent ones (bad text, unrecoverable).

**Proposed approach:** (1) Define an error taxonomy: `TransientError` (GPU OOM, CUDA timeout — retryable), `TextError` (oversize chunk, unparseable character — permanent, fix the text), `VoiceError` (missing latents, corrupt reference — permanent, fix the voice), `SystemError` (disk full, ffmpeg crash — transient but may need human intervention). (2) On `TransientError`, retry with exponential backoff (3 attempts, 30s base). (3) On permanent errors, write the fragment to a dead-letter directory `data/audio/<slug>/dead_letters/` with the error details. (4) The dashboard shows dead letters prominently. (5) `just resynth` can re-process dead letters after the underlying issue is fixed.

**Effort:** M
**Priority:** P1
**Dependencies:** ARCH-01
**Risks:** Retrying GPU OOM without freeing memory first will fail again. Before retry, call `torch.cuda.empty_cache()` and GC.

---

#### DOCKER-01: Production-Grade Containerisation

**Rationale:** *"The CPU images are built and verified. The CUDA images are written against a 12.8 base for Blackwell but have never been built, because those bases are amd64 only."* The current Docker assets are incomplete, the CUDA path is untested, and there are no health checks or resource limits.

**Proposed approach:** (1) **Multi-stage builds:** builder stage installs dependencies, runtime stage copies the installed packages only. Reduces image size by ~40%. (2) **Per-environment images:** `audiobook-narrator`, `audiobook-transcriber`, `audiobook-bookbinder`, `audiobook-studio`. Each runs only its own code. (3) **CUDA image:** Use `nvidia/cuda:12.4.0-runtime-ubuntu22.04` as base. Test on a real GPU in CI. (4) **Health checks:** FastAPI `/health` endpoint checks DB, disk, and (for narrator) GPU availability. (5) **Compose profiles:** `cpu` (bookbinder + studio), `gpu` (all four), `full` (all four + Redis + Jaeger). (6) **Image scanning:** `trivy` or `grype` in CI on every build.

**Effort:** XL
**Priority:** P2
**Dependencies:** ARCH-01, JOB-01
**Risks:** CUDA container debugging is painful. Add a `just docker-shell narrator` that opens a shell in the container with the GPU passed through.

---

#### CONF-01: Configuration Management

**Rationale:** Config is split between `pipeline.toml` (tunables), `cast.yml` (voice assignments), and hardcoded defaults scattered across the codebase. There is no environment-specific override, no secrets integration, and no live reload.

**Proposed approach:** (1) Adopt a layered config: defaults in code → `pipeline.toml` → `pipeline.local.toml` (gitignored) → environment variables. Each layer overrides the previous. (2) Support `AUDIOBOOK_ENV=production|staging|development` to select a base config. (3) For secrets (auth tokens, API keys if added later), read from environment variables exclusively — never from config files. (4) Add a `just config-diff` command that shows the effective config with sources. (5) For the dashboard, support live reload of `cast.yml` without restarting — watch the file and update the in-memory config on change.

**Effort:** M
**Priority:** P1
**Dependencies:** None
**Risks:** The current `pipeline.toml` is simple and well-understood. Don't over-abstract it. Add layers only as needed.

---

#### ARCH-07: Container-Level Dependency Isolation

**Rationale:** The numpy version conflict between environments is the project's most fragile property. The virtualenv split works, but the docs devote extensive space to its failure modes and manual recovery procedures. A developer adding a dependency to the narrator can silently break XTTS by lifting numpy.

**Proposed approach:** As a belt-and-suspenders approach alongside virtualenvs: run each environment in its own container with its own Python, its own numpy, and no possibility of cross-contamination. This is the Docker deployment path (DOCKER-01). For local development, the virtualenv split remains — but add a `just check-isolation` command that verifies each environment's numpy and torch versions match their pinned values and warns on any drift.

**Effort:** M
**Priority:** P2
**Dependencies:** DOCKER-01
**Risks:** This is a defense-in-depth measure, not a replacement for the virtualenv approach. The virtualenv split must remain functional for developers who don't want Docker.

---

## Section 4: Feature Proposals

### Tier 1 — Essential (Makes the tool production-viable)

#### FEAT-01: Pronunciation Dictionary & Custom Phoneme Overrides

**Rationale:** XTTS reads text as-is. Proper nouns, acronyms, and foreign words within a language are frequently mispronounced, and there is no way to correct them without editing the source text. For a 20-hour audiobook, this means dozens of annoying mispronunciations that the listener cannot un-hear.

**Proposed approach:** A pronunciation dictionary: `config/pronunciations.toml` mapping text to phoneme sequences or replacement text. Example: `"Cthulhu" = "kuh-THOO-loo"`, `"NASA" = "Nah-sah"` (for Polish narration). Apply substitutions before synthesis, after chunking. Support per-book overrides. In the dashboard, add a pronunciation editor with a "test" button that synthesises a single word and plays it back. Store the dictionary in the state store.

**Effort:** M
**Priority:** P0
**Dependencies:** None
**Risks:** Text substitution before synthesis is a blunt instrument — it can't handle homographs (read vs read). A true phoneme-level override requires XTTS internals. Start with text substitution; it covers 90% of cases.

---

#### FEAT-02: Automatic Language Detection & Per-Chapter Language

**Rationale:** The current pipeline takes a language flag (e.g., `pl` for Polish) and applies it globally. Books that mix languages (a Polish novel quoting English dialogue, or a technical book with English terms in Polish text) get the wrong prosody for the minority language.

**Proposed approach:** (1) Detect language per chapter using `langdetect` or a fast classifier on the first N characters. (2) Allow per-chapter language overrides in `pipeline.toml` or the dashboard. (3) For mixed-language chapters, detect language per paragraph (slower but more accurate) and set the XTTS language parameter per fragment. (4) The per-fragment language flows through the manifest and is consumed by the narrator.

**Effort:** M
**Priority:** P1
**Dependencies:** ARCH-05
**Risks:** XTTS language switching per fragment may cause jarring voice shifts. Test with real mixed-language books and tune the threshold for per-paragraph detection.

---

#### FEAT-03: Batch Processing & Library Management

**Rationale:** The current workflow is one book at a time. A user with a shelf of 30 books must start each render individually, monitor each separately, and collect outputs one by one. For a small press or a library for the visually impaired, this is untenable.

**Proposed approach:** (1) A queue system: add multiple books to a render queue, each with its own cast and voice. Process sequentially (GPU is the bottleneck). (2) A library view in the dashboard: all books with their status (not started, chunked, rendered, verified, shipped), sortable and filterable. (3) Bulk operations: dry-run all, verify all, export all. (4) A `just queue` command for CLI queue management. (5) Store queue state in the state store (ARCH-01).

**Effort:** L
**Priority:** P1
**Dependencies:** ARCH-01
**Risks:** Queue management complexity. Start simple: FIFO queue, one book at a time, no priority. Add priority and parallel renders (across multiple GPUs) later.

---

#### FEAT-04: Comprehensive Export Formats & Metadata

**Rationale:** Currently supports `.m4b`, `.mp3`, and `.wav`. Commercial audiobook distribution requires specific formats, bitrates, and metadata standards (Audible/ACX, OverDrive, Apple Books). Missing: cover art embedding, ID3 tags, chapter metadata, and LUFS-normalised masters.

**Proposed approach:** (1) Support FLAC and OGG output. (2) Embed cover art from the EPUB or a user-supplied image. (3) Write ID3v2 tags: title, author, narrator (voice name), language, publisher, year, genre ("Audiobook"). (4) Write chapter marks with chapter titles (not just "Chapter 1"). (5) For m4b, write iTunes-compatible audiobook metadata so it appears in the Audiobooks section on Apple devices. (6) Add a `--format` flag to `bin/audiobook` that accepts a comma-separated list for multi-format export.

**Effort:** M
**Priority:** P1
**Dependencies:** None
**Risks:** Metadata standards vary by distributor. Implement the common subset first; add ACX-specific validation as a separate step.

---

#### FEAT-05: Automatic Quality Gating

**Rationale:** *"Nobody listens to twenty hours before publishing."* Verification is currently optional and manual. A book that passes assembly can still have high WER, silent fragments, or truncated text. The system knows how to detect all of these but doesn't refuse to ship a bad book.

**Proposed approach:** (1) After assembly, automatically run verification with sensible defaults (every 20th fragment, WER threshold 0.15). (2) Define a quality gate: max WER, max silence ratio, no oversize chunks, no dead letters, no stale fragments. (3) If the book passes the gate, mark it as `verified` in the state store. If it fails, mark it as `flagged` and show the failures prominently in the dashboard. (4) `bin/audiobook` exits with non-zero if the gate fails. (5) Make the gate configurable in `pipeline.toml` — some users may want to ship with known minor issues.

**Effort:** M
**Priority:** P0
**Dependencies:** ARCH-01, TEST-04
**Risks:** WER baseline varies by voice and language. The default thresholds need to be generous initially and tightened as the system is calibrated.

---

#### FEAT-06: Undo/Redo for Cast & Role Corrections

**Rationale:** *"Corrections are stored against the paragraph, not the fragment number, so they survive re-chunking."* This is good design, but there is no undo. If a user accidentally assigns 50 paragraphs to the wrong voice in the dashboard, they must manually fix each one.

**Proposed approach:** (1) Store role corrections as an append-only event log: each correction records (paragraph_id, old_role, new_role, timestamp, user). (2) Undo replays the log in reverse. Redo replays forward. (3) The dashboard shows a history panel with undo/redo buttons. (4) The event log also serves as an audit trail for multi-user deployments. (5) Keep the current `role_overrides.json` as a materialised view of the event log, for backward compatibility.

**Effort:** M
**Priority:** P1
**Dependencies:** ARCH-01
**Risks:** The append-only log grows. Compact it on book completion (when all corrections are finalised).

---

#### FEAT-07: Real-Time Fragment Preview with Adjustable Parameters

**Rationale:** `just preview` renders 20 fragments to sample the voice, but it doesn't let you adjust speed, emphasis, or pronunciation and immediately hear the result. The current feedback loop is: edit config → re-render 20 fragments → listen → repeat. For a 20-hour book, getting the voice right before committing is critical.

**Proposed approach:** In the dashboard, add a "preview" panel on any book page. Select any fragment, adjust speed (0.8x–1.2x), and click "Preview". The backend synthesises that single fragment with the adjusted parameters and streams the audio back. Response time: ~2s for a single fragment. No files are written to `data/audio/` — this is a throwaway render. When the user is satisfied, "Apply to all" writes the adjusted speed to the cast config.

**Effort:** M
**Priority:** P1
**Dependencies:** None
**Risks:** XTTS inference is not instantaneous. For a long fragment, preview latency could be 5–10s. Show a spinner and consider pre-warming the model.

---

### Tier 2 — High Value (Differentiates from basic TTS wrappers)

#### FEAT-08: Director Mode — Interactive Fragment-by-Fragment Review

**Rationale:** The current workflow is: render all → verify → find problems → re-render individual fragments → re-assemble. This is a batch-and-fix loop. Director mode turns it into a review-and-approve flow: render each fragment, listen, adjust, approve or re-render, then assemble only the approved fragments.

**Proposed approach:** (1) A new dashboard page: "Director". Shows one fragment at a time: text, audio player, role, WER (if verified). (2) Buttons: Approve ✓, Re-render 🔄, Edit text ✏️, Change role, Skip. (3) Re-render applies to only this fragment, with optional parameter overrides (speed +5%, different voice). (4) Progress bar shows approved/total fragments. (5) Assembly is blocked until all fragments are approved (or explicitly skipped). (6) Keyboard shortcuts for fast review: Space=play, →=next, R=re-render, A=approve.

**Effort:** L
**Priority:** P1
**Dependencies:** FEAT-07
**Risks:** A 20-hour book has ~3000 fragments. Reviewing each one takes ~5s = ~4 hours of review. This is still faster than finding problems after the fact. Add a "auto-approve if WER < threshold" option to skip low-risk fragments.

---

#### FEAT-09: Emotion & Style Control Per Paragraph

**Rationale:** XTTS-v2 supports a `style` parameter and emotion conditioning. The current pipeline ignores it — every fragment is rendered with neutral emotion. A horror novel's tense whisper and a comedy's upbeat delivery both sound the same.

**Proposed approach:** (1) Extend the manifest to carry an optional `style` field per fragment. (2) Auto-detect style from text heuristics: exclamation marks → excited, ellipsis → thoughtful, ALL CAPS → emphatic, whispered dialogue markers → quiet. (3) Allow manual override in the dashboard (a dropdown: neutral, happy, sad, angry, whisper, excited). (4) Pass the style to XTTS at synthesis time. (5) Not all TTS backends support style. For backends that don't, silently ignore it.

**Effort:** L
**Priority:** P2
**Dependencies:** ARCH-05
**Risks:** XTTS emotion conditioning quality varies. Test extensively and default to neutral if the styled output is worse (measured by WER against the neutral baseline).

---

#### FEAT-10: Voice Blending & Ageing

**Rationale:** A novel spanning decades may need the same character's voice to age. A series may want a consistent "house style" voice that blends two cloned voices. These are impossible with the current single-voice-per-role model.

**Proposed approach:** (1) Implement speaker latent interpolation: compute latents for voice A and voice B, blend as `α * latent_A + (1 - α) * latent_B`. (2) Expose blend ratio in the cast config: `voice: michal:0.7+kelvin:0.3`. (3) For ageing, blend a voice with a deeper/slower version of itself (obtained by pitch-shifting the reference clips and re-cloning, or by interpolating latents with a bias vector). (4) Cache blended latents to avoid re-computing them per fragment.

**Effort:** L
**Priority:** P2
**Dependencies:** None
**Risks:** Latent interpolation may produce unnatural voices. The blend ratio is not perceptually linear — a 50/50 blend may sound closer to one voice. Add an audition step that synthesises a test phrase with the blended voice before using it in production.

---

#### FEAT-11: Audiobook Mastering Pipeline

**Rationale:** Raw XTTS output is 24 kHz mono with no loudness normalisation, no dynamic range compression, and no noise floor management. Commercial audiobooks must meet loudness standards (typically -16 to -20 LUFS integrated, -1 dB true peak). Without mastering, the audiobook will be noticeably quieter or louder than commercial titles, and quiet passages will be inaudible in noisy environments.

**Proposed approach:** A mastering stage after assembly: (1) **LUFS normalisation** to -19 LUFS integrated (ACX standard) using ffmpeg's `loudnorm` filter (two-pass for accuracy). (2) **True peak limiting** at -1 dB. (3) **Dynamic range compression** (configurable ratio, default 2:1 for audiobook — enough to tame peaks without crushing natural speech dynamics). (4) **Noise floor gating** to remove low-level hiss between words. (5) **Per-format mastering:** m4b gets the full treatment; mp3 gets a louder master for car listening (-16 LUFS); wav is left unmastered for archiving. (6) A `just master <slug>` command and dashboard button. Store mastered files in `data/out/<slug>/` alongside the raw assembly.

**Effort:** M
**Priority:** P1
**Dependencies:** None
**Risks:** Over-compression makes speech sound unnatural. Default to gentle settings and let the user configure. Some voices need different treatment — a booming baritone needs less compression than a quiet whisper.

---

#### FEAT-12: Parallel Fragment Synthesis

**Rationale:** Synthesis runs at 0.4x realtime on an M1, and roughly 3–5x realtime on a decent GPU. A 10-hour book takes 2–3 hours on GPU — but the GPU is idle between fragments (during file I/O, model post-processing). On a multi-GPU machine or a GPU with enough VRAM for batch inference, rendering multiple fragments simultaneously could halve the wall time.

**Proposed approach:** (1) On a single GPU, implement batch inference: collect N fragments, synthesise them in a batch (XTTS supports this via repeated inference calls with different text), write all outputs. N=4 is a reasonable default. (2) On multi-GPU, assign fragment ranges to different GPUs and render in parallel. (3) The progress tracker must handle concurrent fragment completions — use the state store (ARCH-01) with row-level locking. (4) `pipeline.toml` gets `synthesis.parallel_workers = 1` (default, serial) and `synthesis.batch_size = 1`.

**Effort:** L
**Priority:** P2
**Dependencies:** ARCH-01
**Risks:** Batch inference with XTTS may not work — XTTS uses a complex conditioning pipeline (language, style, speaker latent) that may not batch cleanly. Test with the real model before committing to this approach. Multi-GPU is more reliable but requires hardware.

---

#### FEAT-13: SSML & Markdown Annotation Support

**Rationale:** Advanced users want fine-grained control over prosody: pauses, emphasis, pronunciation, and pitch. Currently, the only way to control timing is by adjusting `pipeline.toml` globals, and pronunciation requires editing the source text.

**Proposed approach:** (1) Support a subset of SSML tags in the source text: `<break time="500ms"/>`, `<emphasis>`, `<prosody rate="slow">`. (2) Support Markdown-like annotations: `**word**` for emphasis, `...` for a long pause, `[word](pronunciation)` for pronunciation override. (3) Strip annotations during chunking but record them as per-fragment metadata that the narrator consumes. (4) The narrator interprets annotations: break tags insert silence, emphasis adjusts XTTS style, prosody adjusts speed.

**Effort:** L
**Priority:** P2
**Dependencies:** FEAT-01, FEAT-09
**Risks:** SSML parsing complexity. Implement only the most useful tags first (break and emphasis). Full SSML can come later.

---

#### FEAT-14: Automatic Chapter Splitting & Recombination

**Rationale:** *"Chapter count should match the book. If it is 1, the parser found no headings and the whole book will be one chapter."* Conversely, some books have 80 micro-chapters that make poor audiobook chapters (each is 2 minutes). The user currently has no way to control chapter boundaries without editing the source text.

**Proposed approach:** (1) When the parser finds no headings, attempt to split on blank lines or significant scene breaks (dinkus, asterisms). (2) When there are too many short chapters, offer to recombine: merge consecutive chapters until each meets a minimum duration (configurable, default 10 minutes). (3) When chapters exceed a maximum duration (default 60 minutes), offer to split at paragraph boundaries. (4) Show the proposed chapter structure in the dashboard before synthesis, with a preview of chapter durations. (5) Store chapter structure decisions in the state store so they persist across re-runs.

**Effort:** M
**Priority:** P1
**Dependencies:** None
**Risks:** Recombination changes chapter marks, which affects the listener's navigation experience. Always let the user review and override before proceeding.

---

### Tier 3 — Advanced (Opens new use cases)

#### FEAT-15: REST API for External Integrations

**Rationale:** The dashboard is the only programmatic interface, and it's HTML-first. External tools — editor plugins, batch services, CI pipelines, accessibility tools — need a clean JSON API.

**Proposed approach:** (1) Document all existing FastAPI routes as a JSON API with OpenAPI 3.0 schema (FastAPI generates this automatically). (2) Add missing endpoints: `POST /api/books` (upload + ingest), `POST /api/voices` (upload + clone), `POST /api/renders` (start a render), `GET /api/renders/{id}/status` (poll), `GET /api/renders/{id}/artefacts` (download results). (3) Add API key authentication (separate from dashboard auth) for programmatic access. (4) Rate-limit API keys individually. (5) Publish a Python client library (`audiobook-client`) generated from the OpenAPI schema.

**Effort:** M
**Priority:** P1
**Dependencies:** AUTH-01
**Risks:** The API surface becomes a commitment. Version it (`/api/v1/`) from the start.

---

#### FEAT-16: Fine-Tuning UI with Before/After Comparison

**Rationale:** *"Fine-tuning. Needs CUDA. narrator/train.py refuses to start otherwise."* Fine-tuning is the path to high-quality voice cloning, but the current UX is: edit a config file, run a training script, wait hours, manually compare checkpoint quality. There is no dashboard support for the training loop.

**Proposed approach:** (1) A "Training" section in the dashboard: select a voice, configure training parameters (epochs, learning rate, augmentation), and start training. (2) Live training metrics: loss curve, epoch progress, estimated time remaining (streamed from the trainer via the event bus). (3) After each epoch, auto-generate an audition clip. Display as a before/after comparison: the instant-clone audition vs the fine-tuned audition at epoch N. (4) Checkpoint management: list all checkpoints for a voice, promote one to production, roll back. (5) Dataset curation: listen to and approve/reject individual training segments (the labelled fragments from `data/datasets/<voice>/`).

**Effort:** XL
**Priority:** P2
**Dependencies:** ARCH-02, ARCH-03
**Risks:** Fine-tuning quality is hard to judge automatically. The before/after comparison relies on human ears. Start with the metrics and audition generation; the dataset curation UI is a later refinement.

---

#### FEAT-17: Translation Pipeline

**Rationale:** The system already has TTS and voice cloning. Adding translation creates a full pipeline: English book → translate to Polish → narrate in a Polish cloned voice. This is a compelling product for publishers seeking foreign-language audiobooks.

**Proposed approach:** (1) Integrate an MT model (NLLB, or an API like DeepL for quality). (2) Add a `just translate <slug> <target-lang>` command that translates `chapters.json` while preserving structure and speaker labels. (3) Translation runs before chunking (translate the source text, then chunk in the target language). (4) Store translations in `data/book/<slug>/translations/<lang>/chapters.json`. (5) The dashboard shows a side-by-side view of original and translated text with editable translations. (6) Voice cloning is language-independent — the same voice can narrate in multiple languages if XTTS supports them.

**Effort:** XL
**Priority:** P3
**Dependencies:** FEAT-02
**Risks:** Translation quality varies. Literary translation is particularly hard — idioms, wordplay, and cultural references may be mistranslated. The side-by-side editor is essential for human review. This is a major feature; treat it as a separate project.

---

#### FEAT-18: Plugin System for Custom Processors

**Rationale:** Users will want to integrate new TTS backends (ElevenLabs, Azure, OpenAI), custom text preprocessors (profanity filters for young-readers editions, footnotes to endnotes), or post-filters (profanity masking, content warnings). Currently, each of these requires modifying the source code.

**Proposed approach:** (1) Define plugin interfaces: `TextPreprocessor`, `TTSBackend`, `AudioPostFilter`, `QualityCheck`. Each is a Python class with a standardised interface. (2) Plugins live in `plugins/` and are discovered via entry points (`pyproject.toml [project.entry-points]`). (3) Built-in implementations (XTTS, WhisperX, ffmpeg) are default plugins. (4) Users can add third-party plugins via `pip install audiobook-plugin-elevenlabs`. (5) Configuration in `pipeline.toml` selects active plugins per stage.

**Effort:** XL
**Priority:** P3
**Dependencies:** ARCH-05
**Risks:** Plugin compatibility across numpy versions. Plugins run in the host environment and must be compatible with its dependency constraints. Document the required interface versions clearly.

---

#### FEAT-19: Podcast Mode

**Rationale:** The synthesis pipeline is generic — it produces chaptered audio from text. With minor additions, it becomes a podcast production tool: intro/outro music, segment markers, RSS feed generation.

**Proposed approach:** (1) Add music bed support: configurable intro, outro, and inter-chapter jingles (mixed via ffmpeg). (2) Support segment markers within chapters (ad break points, for podcast monetisation). (3) Generate an RSS 2.0 feed with proper podcast namespace tags (`<itunes:author>`, `<podcast:chapters>`, etc.). (4) Support multi-episode projects: a book serialised as weekly episodes, each 30 minutes, with "previously on..." recaps. (5) Output as MP3 (podcast standard) with ID3 tags.

**Effort:** L
**Priority:** P3
**Dependencies:** FEAT-04, FEAT-11
**Risks:** RSS feed hosting is out of scope — generate the feed XML, let the user host it. Music licensing is the user's responsibility.

---

#### FEAT-20: Multi-Speaker Conversation Mode

**Rationale:** For dialogue-heavy books (plays, screenplays, interview transcripts), the current approach of assigning each line to a voice and rendering independently produces stilted conversation. Real conversation has overlap, pacing, and reactive prosody.

**Proposed approach:** (1) Detect consecutive dialogue turns (alternating speakers). (2) Adjust pacing: reduce pause between turns, add micro-pauses within turns for natural rhythm. (3) Optionally overlap turn boundaries slightly (cross-fade the last 50ms of speaker A with the first 50ms of speaker B, simulating natural interruption). (4) Propagate emotional context: if speaker A is angry, speaker B's response may be defensive. (5) This is an advanced feature; start with pacing adjustment only and add overlap/reactive prosody iteratively.

**Effort:** L
**Priority:** P3
**Dependencies:** FEAT-09
**Risks:** Overlap requires careful cross-fading to avoid artifacts. Test with real dialogue scenes.

---

### Tier 4 — Experimental / Future

#### FEAT-21: Real-Time Streaming Synthesis

**Rationale:** For accessibility (screen readers, live reading assistants) and live narration (public readings, radio), the system needs to synthesise in real-time with sub-second latency, streaming audio as text arrives.

**Proposed approach:** (1) A WebSocket endpoint that accepts text chunks and streams back audio chunks. (2) Use a streaming TTS model (XTTS supports streaming inference with chunked output). (3) Client sends text; server synthesises sentence-by-sentence and streams audio frames. (4) Latency target: <500ms from text received to first audio frame. (5) This requires keeping the model loaded in GPU memory permanently — a different operational model than the current load-render-unload.

**Effort:** XL
**Priority:** P3
**Dependencies:** FEAT-15
**Risks:** Streaming TTS quality may be lower than batch synthesis. Audio artifacts at sentence boundaries. This is a research project — validate with a prototype before committing.

---

#### FEAT-22: Diffusion-Based Voice Editing

**Rationale:** Re-rendering a fragment to fix a mispronunciation takes seconds of GPU time and produces a new rendering that may differ in prosody from the surrounding fragments. An in-place edit — select the word, type the correction, and the model modifies only that segment — would be faster and more consistent.

**Proposed approach:** Research prototype using a diffusion-based audio editing model (e.g., Auffusion or similar). Input: the rendered audio + a text span + replacement text. Output: audio with the span replaced. This is speculative and depends on the maturation of diffusion-based audio editing models.

**Effort:** XL
**Priority:** P3
**Dependencies:** None
**Risks:** The technology may not be mature enough. Evaluate the state of the art before investing.

---

#### FEAT-23: Emotion Detection from Text

**Rationale:** FEAT-09 proposes manual emotion assignment. Automatic detection would reduce the manual work to corrections only.

**Proposed approach:** (1) Train or fine-tune a text emotion classifier on audiobook data (narrated text → emotion labels derived from prosody). (2) Run classification per paragraph during chunking. (3) Store detected emotion in the manifest. (4) The dashboard shows detected emotions as suggestions; the user confirms or overrides. (5) This is an ML project that requires labelled training data.

**Effort:** XL
**Priority:** P3
**Dependencies:** FEAT-09
**Risks:** Emotion from text is ambiguous (the same words can be angry or sad depending on context). This is a research problem. Start with simple heuristic detection and upgrade to ML only when the heuristics hit a ceiling.

---

#### FEAT-24: Multi-Modal Read-Along Synchronisation

**Rationale:** For educational and accessibility applications, synchronising the audiobook with the ebook's page positions enables read-along modes (highlight the current sentence as the audio plays).

**Proposed approach:** (1) During synthesis, record the start time of each fragment in the assembled audiobook. (2) Map fragments back to their source paragraphs and page positions in the EPUB. (3) Export a synchronisation file (SMIL or a custom JSON format) mapping (page, paragraph, character offset) → (audio timestamp). (4) The dashboard demonstrates read-along mode: plays the audio and highlights the current sentence in the text. (5) Export as an EPUB3 Media Overlay for compatibility with read-along readers.

**Effort:** L
**Priority:** P3
**Dependencies:** None
**Risks:** EPUB page positions are not stable across different ebook readers. Character offsets are more reliable. The Media Overlay standard has limited reader support.

---

## Section 5: Developer Experience & Operational Improvements

#### DX-01: Dashboard Front-End Redesign

**Rationale:** The current dashboard is server-rendered templates with no JavaScript framework. The docs note: *"A page rendering with a 200 and containing the right words does not prove the content landed in the right place: a bad edit once put the job table inside the page header and every test still passed."* This is a symptom of an unstructured front-end. Real-time job progress, drag-and-drop uploads, and the Director mode (FEAT-08) require a proper SPA.

**Proposed approach:** (1) Replace Jinja templates with a **Svelte** SPA (lightest runtime, best fit for a mostly-reading dashboard). (2) FastAPI becomes a pure JSON API (FEAT-15). (3) WebSocket for real-time updates: job progress, render status, log streaming. (4) Component library: audio player with waveform, job progress bar, cast editor with dropdowns, fragment reviewer. (5) Build with Vite; serve static assets from FastAPI. (6) Keep the current template tests — they become API contract tests.

**Effort:** XL
**Priority:** P2
**Dependencies:** FEAT-15
**Risks:** This is the single largest item on the roadmap. It touches every page. Mitigate by doing it incrementally: API first, then one page at a time (books → voices → jobs → quality → director).

---

#### DX-02: Unified CLI with Subcommands & Completions

**Rationale:** `just` recipes are powerful but not discoverable (`just --list` shows 40+ recipes with no grouping). The `bin/audiobook` command is better but only covers the single-book workflow. There is no shell completion, no `--help` with examples, and no man page.

**Proposed approach:** (1) Expand `bin/audiobook` into a full CLI with subcommands: `audiobook render`, `audiobook verify`, `audiobook voice clone`, `audiobook voice list`, `audiobook book ingest`, `audiobook book chunk`, `audiobook progress`, `audiobook ui`, `audiobook doctor`. (2) Use `click` or `typer` for the CLI framework (structured help, completion generation). (3) Generate shell completions for bash, zsh, and fish. (4) Each subcommand wraps the corresponding `just` recipe. (5) Keep the justfile for development use; the CLI is the user-facing interface.

**Effort:** M
**Priority:** P1
**Dependencies:** None
**Risks:** The CLI and the justfile must stay in sync. Generate the CLI from the justfile? Too complex. Keep them separate but test that every CLI command invokes the correct recipe.

---

#### DX-03: Developer Onboarding Acceleration

**Rationale:** *"Budget roughly 2.7 GB of virtualenvs plus 1.7 GB of model weights on first synthesis."* That is 4.4 GB before you can do anything. The `just setup` step takes 5–10 minutes on a fast connection. This is a significant barrier for casual contributors.

**Proposed approach:** (1) **Lazy environments:** only install an environment when it's first needed. `just synth` installs the narrator; `just verify` installs the transcriber; `just ui` installs studio. (2) **Devcontainer:** a `.devcontainer.json` for VS Code / GitHub Codespaces that provides a pre-built environment with all dependencies. (3) **Quick-start container:** `docker run audiobook-factory/quickstart` gives a working environment in seconds (pulling a pre-built image). (4) **Model weight streaming:** download XTTS weights in the background while the user works on non-ML tasks. (5) **`just setup --minimal`** that installs only bookbinder and studio (~180 MB, <1 minute).

**Effort:** M
**Priority:** P1
**Dependencies:** DOCKER-01
**Risks:** Lazy environments complicate the justfile. Each recipe must check and offer to install its environment. The devcontainer requires maintaining a Dockerfile.

---

#### DX-04: Debug Bundle & Diagnostic Tooling

**Rationale:** When something goes wrong on a remote GPU box at 3 AM, the developer needs to see: the book's chunk structure, the render report, the progress state, the QA report, the last N lines of the job log, the environment versions, and the problematic audio fragments. Currently, this requires manually locating and reading multiple files across `data/`.

**Proposed approach:** (1) `just debug <slug>` that collects into a single tarball: `chapters.json`, `chunks.jsonl`, `report.json`, `progress.json`, `qa_report.json`, `cast.yml`, the last job log, `just doctor` output, and the flagged audio fragments (the ones with high WER). (2) Redact voice sample paths and personal data. (3) `just debug-verify <slug>` that runs a local dry-run and compares the structure against the remote render. (4) A dashboard "debug" button that downloads the bundle.

**Effort:** S
**Priority:** P1
**Dependencies:** None
**Risks:** Debug bundles could contain sensitive voice data. Redact by default; include audio fragments only with `--include-audio`.

---

#### DX-05: Architecture Decision Records

**Rationale:** `DECISIONS.md` documents why the environments are split and why each pin exists. This is valuable, but it's a single monolithic document that doesn't track decision dates, status, or supersession. As the project evolves, decisions will be revisited (e.g., upgrading XTTS to a numpy-2-compatible version would eliminate the split).

**Proposed approach:** (1) Adopt ADRs (Architecture Decision Records) in `docs/adr/`: numbered Markdown files (`0001-split-environments.md`, `0002-xtts-numpy-1-pin.md`, ...). (2) Each ADR has: Context, Decision, Consequences, Status (proposed/accepted/deprecated/superseded). (3) Migrate DECISIONS.md content into ADRs. (4) Add a `just adr-new` command that creates a template. (5) Keep DECISIONS.md as an index that links to ADRs.

**Effort:** S
**Priority:** P2
**Dependencies:** None
**Risks:** Low risk. This is a documentation refactor. The value is in the discipline of recording decisions as they're made, not retroactively.

---

#### DX-06: Versioning, Changelogs & Releases

**Rationale:** The project has no version number, no changelog, and no release artefacts. Users track `main` and rebuild. For a tool that people depend on for 20-hour renders, breaking changes need to be communicated and rollbacks need to be possible.

**Proposed approach:** (1) Semantic versioning: `MAJOR.MINOR.PATCH`. Breaking changes in the manifest schema or the CLI interface bump MAJOR. New features bump MINOR. Bug fixes bump PATCH. (2) `commitizen` + `conventional commits` for automated changelog generation. (3) GitHub Releases with: changelog, pre-built containers (`audiobook-factory:v0.5.0-cpu`, `audiobook-factory:v0.5.0-gpu`), and a platform-specific bundle for macOS (the primary development platform). (4) `just release` automates: bump version → generate changelog → tag → build containers → create GitHub Release.

**Effort:** M
**Priority:** P2
**Dependencies:** DOCKER-01
**Risks:** The manifest schema version must be tied to the application version. A schema bump without an app version bump will cause cross-environment breakage.

---

## Section 6: Prioritised Implementation Timeline

### Phase 0 — Survival (Weeks 1–2)

*Close the most dangerous security holes and add the tests that catch known failure modes. Everything here can be done without architecture changes.*

| Item | Effort | Why it's in Phase 0 |
|---|---|---|
| AUTH-01: Dashboard token auth | M | No auth → anyone can start renders and read data |
| PATH-01: Path traversal protection | S | Dashboard serves arbitrary files |
| PATH-02: Upload validation | M | Unvalidated uploads to `data/raw/` |
| CMD-01: Subprocess input sanitisation | S | ffmpeg shell-out with unsanitised input |
| TEST-03: Property-based chunking/roles tests | M | Silent book corruption is the #1 quality risk |
| TEST-05: Failure mode regression tests | M | Eight known bugs with no regression coverage |
| TEST-06: Security tests (path traversal, upload) | M | Verify the Phase 0 security fixes work |
| FEAT-01: Pronunciation dictionary | M | The single most requested feature for any TTS tool |
| FEAT-05: Automatic quality gating | M | Nobody listens to 20 hours before shipping |

**Success criteria:** Dashboard requires a token on first access; path traversal and upload exploits fail; all property and regression tests pass in CI; a book with mispronounced proper nouns can be fixed without editing source text; a book that fails WER or silence checks is marked `flagged`, not `verified`.

---

### Phase 1 — Foundation (Weeks 3–6)

*Build the testing and observability infrastructure that makes later feature work safe. Complete the security hardening. Replace the scattered JSON files with a proper state store.*

| Item | Effort | Dependencies |
|---|---|---|
| AUTH-02: Role-based authorisation | M | AUTH-01 |
| NET-01: Network binding & CORS hardening | M | AUTH-01 |
| SEC-02: Model provenance verification | M | None |
| SEC-03: Dependency supply-chain auditing | S | None |
| JOB-01: Job isolation & resource limits | M | None |
| ARCH-01: SQLite state store | L | None |
| ARCH-05: Shared schema package | M | None |
| ARCH-04: Universal resumability | M | ARCH-01 |
| ARCH-06: Error taxonomy & dead letters | M | ARCH-01 |
| CONF-01: Layered configuration | M | None |
| TEST-01: Tiered test strategy | L | None |
| TEST-02: Contract testing | M | ARCH-05 |
| TEST-08: Test infrastructure & fixtures | M | None |
| TEST-04: Audio fidelity testing | L | TEST-01 |
| CI-01: CI pipeline modernisation | M | TEST-01, SEC-03 |
| DX-02: Unified CLI | M | None |
| DX-04: Debug bundle | S | None |
| FEAT-02: Language detection | M | ARCH-05 |
| FEAT-07: Real-time fragment preview | M | None |

**Success criteria:** Every pipeline stage is resumable; errors are classified and retried or dead-lettered; the dashboard shows real-time job state from the state store; the test suite has unit, integration, and E2E tiers; CI runs security audits; model hashes are verified on download; a developer can `just debug <slug>` and get a complete diagnostic bundle.

---

### Phase 2 — Production Features (Weeks 7–12)

*Add the features that make the tool competitive for real audiobook production: mastering, batch processing, comprehensive exports, and the Director mode.*

| Item | Effort | Dependencies |
|---|---|---|
| ARCH-02: Structured logging & observability | L | ARCH-01 |
| SEC-01: Secrets & data protection | L | AUTH-01, PATH-01 |
| FEAT-03: Batch processing & library | L | ARCH-01 |
| FEAT-04: Export formats & metadata | M | None |
| FEAT-06: Undo/redo for corrections | M | ARCH-01 |
| FEAT-08: Director mode | L | FEAT-07 |
| FEAT-11: Audiobook mastering | M | None |
| FEAT-14: Chapter splitting & recombination | M | None |
| FEAT-15: REST API v1 | M | AUTH-01 |
| TEST-07: Performance & concurrency | M | None |
| DX-03: Onboarding acceleration | M | None |
| DX-05: Architecture Decision Records | S | None |

**Success criteria:** A user can queue 10 books for batch rendering, master each to ACX loudness standards, export as m4b with metadata, and review each book in Director mode before shipping. The REST API supports programmatic access. Structured logs with trace IDs make GPU-box debugging tractable.

---

### Phase 3 — Differentiation (Weeks 13–20)

*The SPA dashboard, Docker maturation, voice blending, emotion control, and parallel synthesis.*

| Item | Effort | Dependencies |
|---|---|---|
| DX-01: Dashboard SPA redesign | XL | FEAT-15 |
| DOCKER-01: Production containerisation | XL | ARCH-01, JOB-01 |
| ARCH-03: Event-driven pipeline | L | ARCH-01, ARCH-02 |
| ARCH-07: Container-level isolation | M | DOCKER-01 |
| FEAT-09: Emotion & style control | L | ARCH-05 |
| FEAT-10: Voice blending & ageing | L | None |
| FEAT-12: Parallel fragment synthesis | L | ARCH-01 |
| FEAT-13: SSML & annotation support | L | FEAT-01, FEAT-09 |
| FEAT-16: Fine-tuning UI | XL | ARCH-02, ARCH-03 |
| JOB-02: Container-based job isolation | XL | JOB-01, DOCKER-01 |
| DX-06: Versioning & releases | M | DOCKER-01 |

**Success criteria:** The dashboard is a responsive SPA with WebSocket updates. Docker images are built, scanned, and tested in CI. Voice blending produces audition clips. Emotion control renders demonstrably different prosody. Fine-tuning shows live loss curves and before/after auditions. The project ships versioned releases with pre-built containers.

---

### Phase 4 — Advanced Features (Weeks 21–30)

*Plugin system, translation, podcast mode, conversation mode.*

| Item | Effort | Dependencies |
|---|---|---|
| FEAT-18: Plugin system | XL | ARCH-05 |
| FEAT-17: Translation pipeline | XL | FEAT-02 |
| FEAT-19: Podcast mode | L | FEAT-04, FEAT-11 |
| FEAT-20: Multi-speaker conversation | L | FEAT-09 |
| FEAT-15 extensions: API v2 with plugins | M | FEAT-18 |

**Success criteria:** Third-party TTS backends can be added via plugins. A book can be translated and narrated in a second language. Podcast RSS feeds are generated. Dialogue-heavy books sound more natural with conversation mode.

---

### Phase 5 — Experimental (Ongoing)

*Research projects that may or may not pan out. Validate with prototypes before committing.*

| Item | Effort | Status |
|---|---|---|
| FEAT-21: Real-time streaming synthesis | XL | Prototype first |
| FEAT-22: Diffusion-based voice editing | XL | Research |
| FEAT-23: Emotion detection from text | XL | Research |
| FEAT-24: Read-along synchronisation | L | Prototype first |

**Success criteria:** At least one experimental feature graduates to a supported feature with tests and documentation per quarter.

---

## Appendix: Cross-Reference Dependency Graph

```
AUTH-01 ──▶ AUTH-02, NET-01, SEC-01, FEAT-15
PATH-01 ──▶ SEC-01
PATH-02 ──▶ TEST-06
ARCH-01 ──▶ ARCH-02, ARCH-03, ARCH-04, ARCH-06, FEAT-03, FEAT-06, FEAT-12, TEST-07
ARCH-02 ──▶ ARCH-03, FEAT-16
ARCH-05 ──▶ TEST-02, FEAT-02, FEAT-09, FEAT-13, FEAT-18
DOCKER-01 ──▶ JOB-02, ARCH-07, DX-03, DX-06
FEAT-01  ──▶ FEAT-13
FEAT-07  ──▶ FEAT-08
FEAT-09  ──▶ FEAT-13, FEAT-20
FEAT-11  ──▶ FEAT-19
FEAT-15  ──▶ DX-01
TEST-01  ──▶ TEST-04, CI-01
```

---

## Appendix: Effort Summary by Phase

| Phase | S | M | L | XL | Total items |
|---|---|---|---|---|---|
| 0 | 2 | 7 | 0 | 0 | 9 |
| 1 | 3 | 10 | 3 | 0 | 16 |
| 2 | 1 | 6 | 4 | 0 | 11 |
| 3 | 0 | 2 | 4 | 4 | 10 |
| 4 | 0 | 1 | 2 | 2 | 5 |
| 5 | 0 | 0 | 1 | 3 | 4 |

**Grand total: 55 improvement items across 5 phases.**

The first two phases (26 items, weeks 1–6) transform the project from a clever local tool into something that can be trusted with production work. Everything after that is making it excellent.
