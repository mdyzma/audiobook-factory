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

from studio.data import UnsafeName, check_name

# Actions the dashboard may start, and how each maps to a `just` recipe.
# `args` names the parameters the action takes, in the order the recipe wants
# them. Anything not in this table cannot be run.
ACTIONS: dict[str, dict] = {
    "ingest":   {"recipe": "ingest",   "args": ["source", "slug"],  "locks": False},
    "chunk":    {"recipe": "chunk",    "args": ["slug"],            "locks": False},
    "dryrun":   {"recipe": "dryrun",   "args": ["slug"],            "locks": True},
    "synth":    {"recipe": "synth",    "args": ["slug", "voice"],   "locks": True},
    "resynth":  {"recipe": "resynth",  "args": ["slug", "chunks"],  "locks": True},
    "assemble": {"recipe": "assemble", "args": ["slug", "format"],  "locks": False},
    "verify":   {"recipe": "verify",   "args": ["slug"],            "locks": False},
    "clone":    {"recipe": "clone",    "args": ["voice"],           "locks": False},
    "label":    {"recipe": "label",    "args": ["voice"],           "locks": False},
}

FORMATS = ("m4b", "mp3", "wav")

# A job whose process is gone but which never recorded an exit code was killed
# with the server, or the machine went down.
ORPHANED = "orphaned"


class JobError(RuntimeError):
    """A job that will not be started, with a reason worth showing a user."""


def studio_dir(root: Path) -> Path:
    return root / "data" / ".studio"


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

    @property
    def slug(self) -> str:
        return self.args.get("slug", "")

    @property
    def voice(self) -> str:
        return self.args.get("voice", "")

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

    def acquire(self, slug: str, job_id: str) -> None:
        self.locks.mkdir(parents=True, exist_ok=True)
        held = self.holder(slug)
        if held:
            raise JobError(f"'{slug}' is already being rendered by job {held}")
        self.lock_path(slug).write_text(job_id, encoding="utf-8")

    def release(self, job: Job) -> None:
        if not job.slug:
            return
        path = self.lock_path(job.slug)
        if path.exists() and path.read_text(encoding="utf-8").strip() == job.id:
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

        if spec["locks"]:
            self.store.acquire(clean["slug"], job.id)

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

    def jobs(self) -> list[Job]:
        """Every job, with stored statuses reconciled against reality."""
        reap()
        return self.store.all()

    def tail(self, job_id: str, lines: int = 200) -> str:
        path = self.store.log_path(job_id)
        if not path.exists():
            return ""
        text = path.read_text(encoding="utf-8", errors="replace")
        return "\n".join(text.splitlines()[-lines:])
