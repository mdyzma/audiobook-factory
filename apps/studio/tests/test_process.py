"""The platform seam.

Most of what `process.py` does on Windows cannot be run here. Two things can,
and they are the two worth having:

The Python shim that records a job's exit code is ordinary Python, so it can be
executed on this machine even though it only ever runs on Windows. That is the
part with logic in it, and a wrong exit code would make every finished job look
successful.

The argv each platform builds is a pure function of a flag, so both shapes can
be inspected by pointing the flag the other way.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from studio import process


def test_interpreter_on_posix(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(process, "WINDOWS", False)
    assert process.interpreter(tmp_path).parts[-2:] == ("bin", "python")


def test_interpreter_on_windows(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(process, "WINDOWS", True)
    assert process.interpreter(tmp_path).parts[-2:] == ("Scripts", "python.exe")


@pytest.mark.parametrize("code", [0, 1, 42])
def test_the_windows_shim_records_the_exit_code(tmp_path: Path, code: int) -> None:
    """The shim is Windows-only but not Windows-specific; run it here."""
    exit_file = tmp_path / "status"
    subprocess.run(
        [sys.executable, "-c", process._PY_SHIM, str(exit_file),
         sys.executable, "-c", f"raise SystemExit({code})"],
        check=True,
    )
    assert exit_file.read_text() == str(code)


def test_the_windows_shim_passes_the_child_its_arguments(tmp_path: Path) -> None:
    """An argument that looks like an option must reach the child, not the shim."""
    exit_file = tmp_path / "status"
    written = tmp_path / "seen"
    subprocess.run(
        [sys.executable, "-c", process._PY_SHIM, str(exit_file),
         sys.executable, "-c",
         f"import sys; open(r'{written}','w').write('|'.join(sys.argv[1:]))",
         "--flag", "a b"],
        check=True,
    )
    assert written.read_text() == "--flag|a b"
    assert exit_file.read_text() == "0"


def _recorded(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    seen: list[list[str]] = []

    class Recorder:
        def __init__(self, argv, **kwargs) -> None:
            seen.append(list(argv))
            self.kwargs = kwargs
            self.pid = 1234

    monkeypatch.setattr(process.subprocess, "Popen", Recorder)
    return seen


def test_posix_launch_goes_through_sh(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(process, "WINDOWS", False)
    seen = _recorded(monkeypatch)
    with (tmp_path / "log").open("wb") as log:
        process.launch(["just", "synth", "x"], exit_file=tmp_path / "s",
                       log=log, cwd=tmp_path, env={})
    assert seen[0][:2] == ["/bin/sh", "-c"]
    assert seen[0][-3:] == ["just", "synth", "x"]


def test_windows_launch_avoids_sh(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """There is no dependable /bin/sh on Windows; the shim must be python."""
    monkeypatch.setattr(process, "WINDOWS", True)
    seen = _recorded(monkeypatch)
    with (tmp_path / "log").open("wb") as log:
        process.launch(["just", "synth", "x"], exit_file=tmp_path / "s",
                       log=log, cwd=tmp_path, env={})
    assert seen[0][0] == sys.executable
    assert "/bin/sh" not in seen[0]
    assert seen[0][-3:] == ["just", "synth", "x"]


def test_alive_rejects_a_missing_pid() -> None:
    assert not process.alive(0)
    assert not process.alive(-1)


def test_alive_does_not_signal_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """os.kill(pid, 0) terminates a process on Windows rather than probing it."""
    monkeypatch.setattr(process, "WINDOWS", True)
    monkeypatch.setattr(process, "_alive_windows", lambda pid: True)

    def forbidden(*args, **kwargs):
        raise AssertionError("os.kill must not be used to probe a pid on Windows")

    monkeypatch.setattr(process.os, "kill", forbidden)
    assert process.alive(4321)


def test_terminate_kills_the_tree_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(process, "WINDOWS", True)
    seen: list[list[str]] = []
    monkeypatch.setattr(process.subprocess, "run",
                        lambda argv, **kwargs: seen.append(list(argv)))
    process.terminate(4321)
    assert seen[0][:2] == ["taskkill", "/PID"]
    assert "/T" in seen[0]


class TestExclusive:
    """The file lock the catalog and every run stage take, on this platform.

    Two handles in one process contend exactly as two processes do, with flock
    and with msvcrt alike, so the real lock can be exercised without spawning.
    """

    def test_a_held_lock_refuses_without_waiting(self, tmp_path: Path) -> None:
        lock = tmp_path / ".lock"
        with process.exclusive(lock, wait=False):
            with pytest.raises(BlockingIOError):
                with process.exclusive(lock, wait=False):
                    pass

    def test_it_is_free_again_once_released(self, tmp_path: Path) -> None:
        lock = tmp_path / ".lock"
        with process.exclusive(lock, wait=False):
            pass
        with process.exclusive(lock, wait=False):
            pass

    def test_a_waiting_caller_gets_it_after_release(self, tmp_path: Path) -> None:
        import threading

        lock = tmp_path / ".lock"
        order: list[str] = []
        held = threading.Event()

        def holder() -> None:
            with process.exclusive(lock, wait=False):
                held.set()
                order.append("first")
                threading.Event().wait(0.3)
                order.append("released")

        thread = threading.Thread(target=holder)
        thread.start()
        held.wait(5)
        with process.exclusive(lock, wait=True):
            order.append("second")
        thread.join()
        assert order == ["first", "released", "second"]


def test_terminate_ignores_a_dead_process() -> None:
    """A job that exited between the check and the kill is not an error."""
    process.terminate(0)
