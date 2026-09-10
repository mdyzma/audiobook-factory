"""Run pipeline stages as supervised background jobs.

Three things shape this module.

**It executes commands, so it never accepts one.** A request names an *action*
from a fixed table and supplies arguments that are validated before they reach a
process. There is no path from an HTTP request to an arbitrary command line.

**Jobs outlive the page.** Someone will close the tab during a twelve-hour
render. Each job is a detached process group writing to a log file, and its
metadata is persisted, so a job survives both the browser going away and this
server restarting.

**One render per book.** Two syntheses on the same slug would interleave writes
to the same `rendered.jsonl`. A lock file per slug prevents it.

**One GPU workload at a time.** There is one device, and everything that wants
it wants all of it: a render, a voice clone, and the transcription a quality
check runs are three model loads, not one. Nothing here checks free VRAM, since
the answer would be stale by the time it was acted on. The device is taken like
any other lock, and whoever asks second is told to wait.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from bookbinder.manifest import XTTS_CHAR_LIMITS

from studio.data import UnsafeName, check_name

# What the speech model can actually read. Offering more would fail later.
LANGUAGES = frozenset(XTTS_CHAR_LIMITS)

# Actions the dashboard may start, and how each maps to a `just` recipe.
# `args` names the parameters the action takes, in the order the recipe wants
# them. Anything not in this table cannot be run.
# `scope` and `lock_key` name the thing an action works on. Two jobs on the
# same book, or on the same voice, are never safe to run together: chunking
# rewrites the manifest a render is reading, assembly reads the fragment list a
# render is still appending to, and verification reads audio mid-write.
# `gpu` says the action loads a model onto the device. Clone preparation and
# transcription count as much as synthesis does: `label` and `verify` run ASR,
# and `voice` runs the labelling step on its way to cloning. Leaving those out
# is how a batch survives eight hours and then fails out of memory when a
# quality check lands beside a render.
ACTIONS: dict[str, dict] = {
    # Cleans a recording, cuts and labels it, then clones the voice. Minutes,
    # not seconds: the labelling step downloads a speech model the first time.
    "voice":    {"recipe": "voice",    "args": ["sample", "name", "language"],
                 "locks": True, "lock_key": "name", "scope": "voice", "gpu": True},
    "ingest":   {"recipe": "ingest",   "args": ["source", "slug"],  "locks": False},
    "chunk":    {"recipe": "chunk",    "args": ["slug"],            "locks": False},
    # Silence at the estimated durations, written by bookbinder, which has no
    # torch at all. It locks the book but never touches the device.
    "dryrun":   {"recipe": "dryrun",   "args": ["slug"],            "locks": True},
    "synth":    {"recipe": "synth",    "args": ["slug", "voice"],   "locks": True,
                 "gpu": True},
    "resynth":  {"recipe": "resynth",  "args": ["slug", "chunks"],  "locks": True,
                 "gpu": True},
    "assemble": {"recipe": "assemble", "args": ["slug", "format"],  "locks": False},
    "verify":   {"recipe": "verify",   "args": ["slug"],            "locks": False,
                 "gpu": True},
    "clone":    {"recipe": "clone",    "args": ["voice"],           "locks": False,
                 "lock_key": "voice", "scope": "voice", "gpu": True},
    "label":    {"recipe": "label",    "args": ["voice"],           "locks": False,
                 "lock_key": "voice", "scope": "voice", "gpu": True},
}

# Everything that wants the one device.
GPU_ACTIONS = frozenset(name for name, spec in ACTIONS.items() if spec.get("gpu"))

# The device, as a lock. Namespaced like every other lock name, so no book or
# voice can ever be called the same thing.
DEVICE_LOCK = "device-gpu"

DEVICE_BUSY = (
    "the GPU is busy: job {job} has it. Renders, voice cloning and "
    "transcription each load a model onto the one device, so they take turns."
)

FORMATS = ("m4b", "mp3", "wav")

# A job whose process is gone but which never recorded an exit code was killed
# with the server, or the machine went down.
ORPHANED = "orphaned"


class JobError(RuntimeError):
    """A job that will not be started, with a reason worth showing a user."""


def studio_dir(root: Path) -> Path:
    return root / "data" / ".studio"


def lock_name(scope: str, target: str) -> str:
    """A lock name that says what it protects, not just what it is called.

    Without the prefix a voice and a book of the same name share one lock file,
    and cloning `solaris` reports the book of that name as already rendering.
    Prefixing every lock makes the mapping from thing to lock injective.
    """
    return f"{scope}-{check_name(target)}"


@dataclass
class Job:
    id: str
    action: str
    args: dict[str, str]
    status: str = "running"          # running | succeeded | failed | cancelled | orphaned
    pid: int = 0
    started_at: str = ""
    finished_at: str = ""
    exit_code: int | None = None
    command: list[str] = field(default_factory=list)
    # Every lock this job took, so finishing gives all of them back. A job may
    # hold both its book and the device, and releasing one of the two leaves
    # the batch wedged behind the other.
    locks_held: list[str] = field(default_factory=list)

    @property
    def slug(self) -> str:
        return self.args.get("slug", "")

    @property
    def voice(self) -> str:
        # `voice` for actions that use one, `name` for the action that makes one.
        return self.args.get("voice") or self.args.get("name", "")

    @property
    def lock_target(self) -> str:
        spec = ACTIONS.get(self.action) or {}
        return self.args.get(spec.get("lock_key", "slug"), "")

    @property
    def lock_name(self) -> str:
        spec = ACTIONS.get(self.action) or {}
        target = self.lock_target
        return lock_name(spec.get("scope", "book"), target) if target else ""

    @property
    def uses_gpu(self) -> bool:
        return bool((ACTIONS.get(self.action) or {}).get("gpu"))

    @property
    def conflict_key(self) -> str:
        """The book or voice this job has to itself while it runs.

        Namespaced, so a book and a voice that happen to share a name are not
        mistaken for the same resource.
        """
        spec = ACTIONS.get(self.action) or {}
        target = self.lock_target
        return f"{spec.get('scope', 'book')}:{target}" if target else ""

    @property
    def running(self) -> bool:
        return self.status == "running"


def _child_env() -> dict[str, str]:
    """Environment for a job.

    VIRTUAL_ENV and friends are stripped: this server runs inside studio's own
    virtualenv, and leaking it makes uv warn on every single job that the active
    environment does not match the project it is about to run. The jobs each
    resolve their own environment through `just`.
    """
    env = {k: v for k, v in os.environ.items()
           if k not in ("VIRTUAL_ENV", "PYTHONHOME", "PYTHONPATH", "UV_PROJECT_ENVIRONMENT")}
    # XTTS refuses to download without this, and a job has no terminal to ask at.
    env["COQUI_TOS_AGREED"] = "1"
    return env


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _alive(pid: int) -> bool:
    """Whether a pid is still a live process.

    A finished child that nobody has waited on stays a zombie and answers
    signal 0, so this returns True until it is reaped. `reap()` clears them,
    and the exit file is checked before this in any case.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def reap() -> int:
    """Clear finished children so they stop counting as alive.

    Jobs are started and forgotten across separate HTTP requests, so nothing
    waits on them. Without this, every completed render leaves a zombie for the
    lifetime of the server.
    """
    reaped = 0
    while True:
        try:
            pid, _status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            break
        except OSError:
            break
        if pid == 0:
            break
        reaped += 1
    return reaped


class JobStore:
    """Job metadata and logs on disk, so nothing depends on this process."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.dir = studio_dir(root) / "jobs"
        self.locks = studio_dir(root) / "locks"

    def _path(self, job_id: str) -> Path:
        return self.dir / f"{check_name(job_id)}.json"

    def exit_path(self, job_id: str) -> Path:
        return self.dir / f"{check_name(job_id)}.exit"

    def log_path(self, job_id: str) -> Path:
        return self.dir / f"{check_name(job_id)}.log"

    def save(self, job: Job) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self._path(job.id).with_suffix(".json.tmp")
        tmp.write_text(json.dumps(asdict(job), indent=2), encoding="utf-8")
        tmp.replace(self._path(job.id))

    def load(self, job_id: str) -> Job | None:
        path = self._path(job_id)
        if not path.exists():
            return None
        try:
            return self._reconcile(Job(**json.loads(path.read_text(encoding="utf-8"))))
        except (json.JSONDecodeError, TypeError):
            return None

    def all(self) -> list[Job]:
        if not self.dir.is_dir():
            return []
        jobs = []
        for path in self.dir.glob("*.json"):
            job = self.load(path.stem)
            if job:
                jobs.append(job)
        return sorted(jobs, key=lambda j: j.started_at, reverse=True)

    def _reconcile(self, job: Job) -> Job:
        """Correct a stored status against reality.

        A detached process cannot be waited on from a later request, so its
        exit code is written to a file by the wrapper. Three cases:

        - the exit file exists: the job ran to completion, code included;
        - no exit file and the process is gone: it was killed, or the machine
          went down. Saying `orphaned` beats showing a render that will never
          finish as still running;
        - otherwise it is genuinely still going.
        """
        if job.status != "running":
            return job

        exit_file = self.exit_path(job.id)
        if exit_file.exists():
            try:
                job.exit_code = int(exit_file.read_text(encoding="utf-8").strip() or 1)
            except ValueError:
                job.exit_code = 1
            job.status = "succeeded" if job.exit_code == 0 else "failed"
            job.finished_at = job.finished_at or _now()
            self.save(job)
            self.release(job)
        elif not _alive(job.pid):
            job.status = ORPHANED
            job.finished_at = job.finished_at or _now()
            self.save(job)
            self.release(job)
        return job

    # --- locking -------------------------------------------------------------

    def lock_path(self, slug: str) -> Path:
        return self.locks / f"{check_name(slug)}.lock"

    def holder(self, slug: str) -> str | None:
        """The job id currently rendering this book, if any."""
        path = self.lock_path(slug)
        if not path.exists():
            return None
        job_id = path.read_text(encoding="utf-8").strip()
        job = self.load(job_id) if job_id else None
        if job and job.running:
            return job_id
        # Stale lock: the holder is gone.
        path.unlink(missing_ok=True)
        return None

    def acquire(self, name: str, job_id: str, refusal: str = "") -> None:
        """Take a named lock, or refuse.

        Created with O_EXCL because check-then-write loses the race: two
        requests arriving together both saw no holder, both wrote, and the two
        renders then shared one audio directory and one rendered.jsonl.

        `refusal` is the message to raise when someone else holds it, with
        `{job}` for the holder. The device needs a different sentence from a
        book, and the person reading it is the one who clicked the button.
        """
        self.locks.mkdir(parents=True, exist_ok=True)
        path = self.lock_path(name)
        template = refusal or "'{name}' is already being rendered by job {job}"
        for _attempt in range(2):
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                # Either a live holder, or a lock whose owner died. `holder`
                # clears the second case, so one retry is enough.
                held = self.holder(name)
                if held:
                    raise JobError(template.format(job=held, name=name))
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(job_id)
            return
        raise JobError(f"could not take the '{name}' lock")

    def device_holder(self) -> str | None:
        """The job using the GPU, if one is."""
        return self.holder(DEVICE_LOCK)

    def conflicting_job(self, key: str) -> Job | None:
        """A running job already working on the same book or voice."""
        if not key:
            return None
        return next((j for j in self.all() if j.running and j.conflict_key == key), None)

    def release(self, job: Job) -> None:
        """Give back everything this job took.

        `locks_held` is authoritative for jobs started since it existed; the
        fallback covers records written before, which knew only one lock.
        """
        for name in job.locks_held or ([job.lock_name] if job.lock_target else []):
            self.release_lock(name, job.id)

    def release_lock(self, name: str, job_id: str) -> None:
        """Unlink a lock this job holds, and leave anyone else's alone."""
        path = self.lock_path(name)
        if path.exists() and path.read_text(encoding="utf-8").strip() == job_id:
            path.unlink(missing_ok=True)


class JobRunner:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.store = JobStore(root)

    def validate(self, action: str, args: dict[str, str]) -> dict[str, str]:
        spec = ACTIONS.get(action)
        if spec is None:
            raise JobError(f"unknown action '{action}'")

        clean: dict[str, str] = {}
        for name in spec["args"]:
            value = (args.get(name) or "").strip()
            if name == "format":
                if value and value not in FORMATS:
                    raise JobError(f"format must be one of {', '.join(FORMATS)}")
                clean[name] = value
                continue
            if name == "voice" and action == "synth" and not value:
                clean[name] = ""       # use the cast recorded in book.json
                continue
            if name == "chunks":
                # A comma-separated list, each item validated on its own: this
                # becomes an argument to a command.
                ids = [c.strip() for c in value.split(",") if c.strip()]
                if not ids:
                    raise JobError("no fragments given")
                if len(ids) > 200:
                    raise JobError("too many fragments at once; re-render the book")
                for chunk_id in ids:
                    try:
                        check_name(chunk_id)
                    except UnsafeName:
                        raise JobError(f"invalid fragment id: {chunk_id!r}")
                clean[name] = ",".join(ids)
                continue
            if name == "sample":
                clean[name] = self._raw_voice(value)
                continue
            if name == "language":
                code = value.lower() or "pl"
                if code not in LANGUAGES:
                    raise JobError(
                        f"unsupported language '{value}'; the model handles "
                        + ", ".join(sorted(LANGUAGES))
                    )
                clean[name] = code
                continue
            if name == "source":
                # A file already inside data/raw/books, never an arbitrary path:
                # the caller picks from what has been uploaded.
                clean[name] = self._raw_book(value)
                continue
            try:
                clean[name] = check_name(value)
            except UnsafeName:
                raise JobError(f"invalid {name}: {value!r}")
        return clean

    def _raw_voice(self, name: str) -> str:
        from studio.authoring import VOICE_SUFFIXES

        candidate = Path(name)
        if candidate.name != name or candidate.suffix.lower() not in VOICE_SUFFIXES:
            raise JobError(f"invalid sample file: {name!r}")
        path = self.root / "data" / "raw" / "voices" / candidate.name
        if not path.is_file():
            raise JobError(f"no uploaded sample named {name!r}")
        return str(path.relative_to(self.root))

    def _raw_book(self, name: str) -> str:
        from studio.authoring import BOOK_SUFFIXES

        candidate = Path(name)
        if candidate.name != name or candidate.suffix.lower() not in BOOK_SUFFIXES:
            raise JobError(f"invalid source file: {name!r}")
        path = self.root / "data" / "raw" / "books" / candidate.name
        if not path.is_file():
            raise JobError(f"no uploaded book named {name!r}")
        return str(path.relative_to(self.root))

    def start(self, action: str, args: dict[str, str]) -> Job:
        spec = ACTIONS[action] if action in ACTIONS else None
        clean = self.validate(action, args)
        assert spec is not None

        job = Job(
            id=uuid.uuid4().hex[:12],
            action=action,
            args=clean,
            started_at=_now(),
            command=["just", spec["recipe"], *[clean[a] for a in spec["args"]]],
        )

        # No two jobs on one book, whether or not either takes the render lock.
        # Chunking during a render rewrites the manifest under it, assembling
        # reads a fragment list still being appended to, and verifying reads
        # audio mid-write. None of those failed loudly.
        busy = self.store.conflicting_job(job.conflict_key)
        if busy is not None:
            raise JobError(
                f"'{busy.lock_target}' is busy: job {busy.id} is running "
                f"'{busy.action}'. Wait for it, or cancel it first."
            )

        # Book or voice first, then the device. Neither call waits, so there is
        # no deadlock to order around; what matters is giving back the first
        # lock when the second is refused, or a book stays locked by a job that
        # never started.
        taken: list[str] = []
        try:
            if spec["locks"]:
                # Most actions lock the book they render; creating a voice locks
                # the voice instead, so two runs cannot build the same one.
                self.store.acquire(job.lock_name, job.id)
                taken.append(job.lock_name)
            if job.uses_gpu:
                self.store.acquire(DEVICE_LOCK, job.id, DEVICE_BUSY)
                taken.append(DEVICE_LOCK)
        except JobError:
            for name in taken:
                self.store.release_lock(name, job.id)
            raise
        job.locks_held = taken

        self.store.dir.mkdir(parents=True, exist_ok=True)
        log = self.store.log_path(job.id)
        exit_file = self.store.exit_path(job.id)
        exit_file.unlink(missing_ok=True)

        # A detached process cannot be waited on later, so record the exit code
        # where a future request can read it. Arguments go through "$@" rather
        # than into the script text: they are validated already, but building a
        # shell string out of user input is not a habit worth having.
        wrapper = 'exec_status=0; "$@" || exec_status=$?; printf %s "$exec_status" > "$0"'
        try:
            with log.open("wb") as handle:
                process = subprocess.Popen(
                    ["/bin/sh", "-c", wrapper, str(exit_file), *job.command],
                    cwd=self.root,
                    stdout=handle,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    # Its own process group, so closing the browser, or this
                    # server exiting, does not take a twelve-hour render with it.
                    start_new_session=True,
                    env=_child_env(),
                )
        except OSError as exc:
            self.store.release(job)
            raise JobError(f"could not start: {exc}") from exc

        job.pid = process.pid
        self.store.save(job)
        return job

    def cancel(self, job_id: str) -> Job:
        reap()
        job = self.store.load(job_id)
        if job is None:
            raise JobError(f"no job {job_id}")
        if not job.running:
            return job
        try:
            # The whole group: `just` spawns uv, which spawns python.
            os.killpg(os.getpgid(job.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        job.status = "cancelled"
        job.finished_at = _now()
        self.store.save(job)
        self.store.release(job)
        return job

    def device_busy(self) -> str | None:
        """The job holding the GPU, if any.

        A queue worker asks this before claiming, so a busy device makes it
        take the next book's chapter split instead of waiting on the render.
        """
        reap()
        return self.store.device_holder()

    def jobs(self) -> list[Job]:
        """Every job, with stored statuses reconciled against reality."""
        reap()
        return self.store.all()

    def prune(self, keep: int = 20, remove_all: bool = False) -> dict:
        """Delete finished jobs and their logs.

        Nothing prunes on its own, and a long render's log grows with every
        progress line, so this exists to be run occasionally.

        A running job is never touched: its log is still being written and its
        lock still means something. Stale locks left by jobs that died are
        cleared, since those do block the next render.
        """
        reap()
        jobs = self.store.all()
        finished = [j for j in jobs if not j.running]
        running = [j for j in jobs if j.running]

        doomed = finished if remove_all else finished[keep:]  # all() is newest first
        freed = 0
        for job in doomed:
            for path in (self.store._path(job.id), self.store.log_path(job.id),
                         self.store.exit_path(job.id)):
                if path.exists():
                    freed += path.stat().st_size
                    path.unlink()
            self.store.release(job)

        # A lock whose holder is gone would otherwise refuse the next render.
        stale_locks = 0
        if self.store.locks.is_dir():
            for lock in self.store.locks.glob("*.lock"):
                if self.store.holder(lock.stem) is None and lock.exists():
                    lock.unlink()
                    stale_locks += 1

        return {"removed": len(doomed), "kept": len(finished) - len(doomed),
                "running": len(running), "bytes_freed": freed,
                "stale_locks_cleared": stale_locks}

    def tail(self, job_id: str, lines: int = 200) -> str:
        path = self.store.log_path(job_id)
        if not path.exists():
            return ""
        text = path.read_text(encoding="utf-8", errors="replace")
        return "\n".join(text.splitlines()[-lines:])
