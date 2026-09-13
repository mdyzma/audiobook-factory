"""Starting, watching and stopping detached jobs, on both kinds of machine.

Every place the two operating systems disagree about processes lives here, so
that `jobs.py` can read as one story rather than as a pair of branches. There
are four disagreements and each one is sharper than it first looks.

**Asking whether a pid is alive.** POSIX has signal 0 for exactly this. Windows
does not: `os.kill(pid, 0)` there does not ask a question, it calls
`TerminateProcess` with 0 as the exit code. Using the POSIX idiom on Windows
would kill every job the dashboard tried to look at. The Windows path opens the
process and reads its exit code instead.

**Stopping a job.** `just` spawns uv, which spawns python, so stopping the
process we started is not enough. POSIX has the process group. Windows has no
equivalent that survives a detached process with no console, so the tree is
killed by `taskkill /T`.

**Recording the exit code.** A detached process cannot be waited on later, so a
shim writes the status where a future request can read it. On POSIX that shim is
`sh`. Windows has no `sh` that can be relied on — scoop and Git Bash put one
somewhere, but not at a fixed path — so the shim is Python, which is
unambiguously present because it is what is running.

**Reaping.** A POSIX child that nobody waits on stays a zombie and keeps
answering signal 0. Windows has no such state and nothing to reap.

**Holding a file lock.** `fcntl.flock` does not exist on Windows, and importing
`fcntl` there fails outright, which took every catalog import and every run
stage down with it. Windows locks a byte range with `msvcrt.locking` instead.

The Windows halves were written from the documented behaviour of the APIs and
first ran on the RTX workstation.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Iterator, Sequence

WINDOWS = sys.platform == "win32"

# Arguments go through "$@" rather than into the script text: they are validated
# already, but building a shell string out of user input is not a habit worth
# having. $0 carries the exit file for the same reason.
_SH_SHIM = 'exec_status=0; "$@" || exec_status=$?; printf %s "$exec_status" > "$0"'

# The same contract in Python, for the platform with no dependable sh. stdout
# and stderr are inherited, so the child still writes straight into the log.
_PY_SHIM = (
    "import subprocess,sys;"
    "code=subprocess.call(sys.argv[2:]);"
    "open(sys.argv[1],'w').write(str(code))"
)

# subprocess only defines these on Windows, so they cannot be referenced by
# name from a module that also has to import cleanly on macOS and Linux.
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200

# GetExitCodeProcess reports this for a process that has not finished.
_STILL_ACTIVE = 259
# Enough to read the exit code, and grantable for processes this one did not
# start, which PROCESS_QUERY_INFORMATION is not always.
_QUERY_LIMITED = 0x1000


@contextmanager
def exclusive(path: Path, *, wait: bool) -> Iterator[None]:
    """Hold an exclusive lock on `path` for the duration of the block.

    With `wait`, block until the lock is free. Without it, raise
    BlockingIOError at once if another process holds it. The lock belongs to
    the open file, so a process that dies releases it on both platforms.
    """
    with path.open("a+b") as handle:
        if WINDOWS:
            import msvcrt

            # The first byte stands for the whole file. Locking past the end
            # is allowed, and nothing is ever written to a lock file.
            handle.seek(0)
            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
                    break
                except OSError as exc:
                    if not wait:
                        raise BlockingIOError(str(exc)) from exc
                    time.sleep(0.05)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
        else:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[attr-defined]
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)  # type: ignore[attr-defined]


def interpreter(environment: Path) -> Path:
    """The environment's own python, where uv puts it on this platform."""
    if WINDOWS:
        return environment / ".venv" / "Scripts" / "python.exe"
    return environment / ".venv" / "bin" / "python"


def alive(pid: int) -> bool:
    """Whether a pid is still a live process.

    On POSIX a finished child that nobody has waited on stays a zombie and
    answers signal 0, so this returns True until it is reaped. `reap()` clears
    them, and the exit file is checked before this in any case.

    On Windows the same caveat arrives differently: a process whose own exit
    code happens to be 259 is indistinguishable from a running one. The exit
    file being checked first is what makes that survivable rather than a bug.
    """
    if pid <= 0:
        return False
    if WINDOWS:
        return _alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _alive_windows(pid: int) -> bool:
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]

    handle = kernel32.OpenProcess(_QUERY_LIMITED, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def reap() -> int:
    """Clear finished children so they stop counting as alive.

    Jobs are started and forgotten across separate HTTP requests, so nothing
    waits on them. Without this, every completed render leaves a zombie for the
    lifetime of the server. Windows has no zombies and nothing to do here.
    """
    if WINDOWS:
        return 0
    reaped = 0
    while True:
        try:
            pid, _status = os.waitpid(-1, os.WNOHANG)  # type: ignore[attr-defined]
        except ChildProcessError:
            break
        except OSError:
            break
        if pid == 0:
            break
        reaped += 1
    return reaped


def launch(
    command: Sequence[str],
    *,
    exit_file: Path,
    log: IO[bytes],
    cwd: Path,
    env: dict[str, str],
) -> subprocess.Popen[bytes]:
    """Start a job detached from this server, writing its status to `exit_file`.

    Detached in both senses: closing the browser does not stop a twelve-hour
    render, and neither does restarting this server.
    """
    if WINDOWS:
        argv = [sys.executable, "-c", _PY_SHIM, str(exit_file), *command]
        # No console to inherit and its own group, which is as close as Windows
        # comes to the POSIX session below.
        extra = {"creationflags": _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP}
    else:
        argv = ["/bin/sh", "-c", _SH_SHIM, str(exit_file), *command]
        # Its own process group, so closing the browser, or this server
        # exiting, does not take a twelve-hour render with it.
        extra = {"start_new_session": True}
    return subprocess.Popen(
        argv,
        cwd=cwd,
        stdout=log,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        env=env,
        **extra,  # type: ignore[arg-type]
    )


def terminate(pid: int) -> None:
    """Stop a job and everything it started, or do nothing if it is already gone.

    `just` spawns uv, which spawns python, so the whole tree has to go.
    """
    if pid <= 0:
        return
    if WINDOWS:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)  # type: ignore[attr-defined]
    except (ProcessLookupError, PermissionError):
        pass
