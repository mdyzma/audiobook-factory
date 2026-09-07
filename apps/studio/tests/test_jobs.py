"""Job supervision.

This is the module that runs commands, so the validation tests matter most:
there must be no path from a request to a command line the table does not name.
"""

from __future__ import annotations

import json
import os
import time

import pytest

from studio.jobs import ACTIONS, Job, JobError, JobRunner, JobStore, _child_env, reap


@pytest.fixture
def runner(project):
    return JobRunner(project)


class TestValidation:
    def test_unknown_action_is_refused(self, runner):
        with pytest.raises(JobError, match="unknown action"):
            runner.validate("rm", {"slug": "x"})

    @pytest.mark.parametrize("slug", [
        "../etc", "a/b", "", "a;rm -rf /", "a b", "$(whoami)", "a|b", "..",
    ])
    def test_shell_metacharacters_never_reach_a_command(self, runner, slug):
        with pytest.raises(JobError, match="invalid slug"):
            runner.validate("chunk", {"slug": slug})

    def test_voice_is_validated_too(self, runner):
        with pytest.raises(JobError, match="invalid voice"):
            runner.validate("clone", {"voice": "../../etc/passwd"})

    def test_format_must_be_a_known_container(self, runner):
        with pytest.raises(JobError, match="format must be"):
            runner.validate("assemble", {"slug": "solaris", "format": "exe"})

    def test_empty_voice_means_use_the_recorded_cast(self, runner):
        # `just synth <slug> ""` is how the pipeline says "use book.json".
        assert runner.validate("synth", {"slug": "solaris", "voice": ""})["voice"] == ""

    def test_valid_arguments_pass(self, runner):
        assert runner.validate("chunk", {"slug": "solaris"}) == {"slug": "solaris"}

    def test_every_action_maps_to_a_recipe(self):
        for name, spec in ACTIONS.items():
            assert spec["recipe"] and isinstance(spec["args"], list), name


class TestChildEnvironment:
    def test_strips_the_servers_own_virtualenv(self, monkeypatch):
        # Leaking it makes uv warn on every job that the active environment
        # does not match the project it is about to run.
        monkeypatch.setenv("VIRTUAL_ENV", "/somewhere/studio/.venv")
        assert "VIRTUAL_ENV" not in _child_env()

    def test_agrees_to_the_model_licence(self):
        # A job has no terminal, so XTTS cannot ask.
        assert _child_env()["COQUI_TOS_AGREED"] == "1"


class TestLifecycle:
    def _run(self, runner, action, args, timeout=20):
        job = runner.start(action, args)
        for _ in range(timeout * 4):
            time.sleep(0.25)
            current = runner.store.load(job.id)
            if current and not current.running:
                return current
        return runner.store.load(job.id)

    def test_a_job_records_its_exit_code(self, runner, monkeypatch):
        # `just` is not on PATH inside the temp project, so this fails, which
        # is exactly what a non-zero exit code should look like.
        job = self._run(runner, "chunk", {"slug": "solaris"})
        assert job is not None
        assert job.status in ("failed", "succeeded")
        assert job.exit_code is not None

    def test_log_is_captured(self, runner):
        job = self._run(runner, "chunk", {"slug": "solaris"})
        assert job is not None
        assert isinstance(runner.tail(job.id), str)

    def test_cancel_on_an_unknown_job(self, runner):
        with pytest.raises(JobError, match="no job"):
            runner.cancel("deadbeef1234")


class TestLocking:
    def test_a_second_render_is_refused(self, project):
        store = JobStore(project)
        store.dir.mkdir(parents=True, exist_ok=True)
        # A live holder: this test's own process, which is certainly alive.
        holder = Job(id="aaaaaaaaaaaa", action="synth", args={"slug": "solaris"},
                     status="running", pid=os.getpid(), started_at="2026-01-01T00:00:00+00:00")
        store.save(holder)
        store.acquire("solaris", holder.id)

        with pytest.raises(JobError, match="already being rendered"):
            JobRunner(project).start("synth", {"slug": "solaris", "voice": ""})

    def test_a_lock_held_by_a_dead_job_is_cleared(self, project):
        store = JobStore(project)
        store.dir.mkdir(parents=True, exist_ok=True)
        dead = Job(id="bbbbbbbbbbbb", action="synth", args={"slug": "solaris"},
                   status="running", pid=999999, started_at="2026-01-01T00:00:00+00:00")
        store.save(dead)
        store.locks.mkdir(parents=True, exist_ok=True)
        store.lock_path("solaris").write_text(dead.id, encoding="utf-8")

        # Nothing should stay locked by a process that no longer exists.
        assert store.holder("solaris") is None

    def test_non_rendering_actions_do_not_lock(self):
        assert ACTIONS["assemble"]["locks"] is False
        assert ACTIONS["verify"]["locks"] is False
        assert ACTIONS["synth"]["locks"] is True
        assert ACTIONS["dryrun"]["locks"] is True


class TestReconciliation:
    def test_a_job_whose_process_vanished_is_orphaned(self, project):
        store = JobStore(project)
        store.dir.mkdir(parents=True, exist_ok=True)
        store.save(Job(id="cccccccccccc", action="synth", args={"slug": "solaris"},
                       status="running", pid=999999,
                       started_at="2026-01-01T00:00:00+00:00"))
        # No exit file and no process: it was killed, not finished.
        job = store.load("cccccccccccc")
        assert job is not None and job.status == "orphaned"

    def test_an_exit_file_decides_success(self, project):
        store = JobStore(project)
        store.dir.mkdir(parents=True, exist_ok=True)
        store.save(Job(id="dddddddddddd", action="chunk", args={"slug": "solaris"},
                       status="running", pid=999999,
                       started_at="2026-01-01T00:00:00+00:00"))
        store.exit_path("dddddddddddd").write_text("0", encoding="utf-8")
        job = store.load("dddddddddddd")
        assert job is not None
        assert job.status == "succeeded" and job.exit_code == 0

    def test_a_non_zero_exit_file_means_failure(self, project):
        store = JobStore(project)
        store.dir.mkdir(parents=True, exist_ok=True)
        store.save(Job(id="eeeeeeeeeeee", action="chunk", args={"slug": "solaris"},
                       status="running", pid=999999,
                       started_at="2026-01-01T00:00:00+00:00"))
        store.exit_path("eeeeeeeeeeee").write_text("2", encoding="utf-8")
        job = store.load("eeeeeeeeeeee")
        assert job is not None
        assert job.status == "failed" and job.exit_code == 2


class TestReaping:
    def test_reap_clears_finished_children(self):
        import subprocess
        from studio.jobs import _alive
        p = subprocess.Popen(["/bin/sh", "-c", "exit 0"], start_new_session=True)
        time.sleep(0.5)
        # Until reaped, a finished child is a zombie and still answers signal 0.
        assert _alive(p.pid)
        reap()
        assert not _alive(p.pid)


class TestResynth:
    """Re-rendering named fragments, which is how a flagged one gets fixed."""

    def test_accepts_a_list_of_fragment_ids(self, runner):
        clean = runner.validate("resynth", {"slug": "solaris",
                                            "chunks": "ch001_0004, ch002_0041"})
        assert clean["chunks"] == "ch001_0004,ch002_0041"

    def test_refuses_an_empty_list(self, runner):
        with pytest.raises(JobError, match="no fragments"):
            runner.validate("resynth", {"slug": "solaris", "chunks": " , "})

    @pytest.mark.parametrize("bad", ["../etc", "a;rm -rf /", "a/b", "a b"])
    def test_every_id_is_validated_individually(self, runner, bad):
        with pytest.raises(JobError, match="invalid fragment id"):
            runner.validate("resynth", {"slug": "solaris", "chunks": f"ch001_0001,{bad}"})

    def test_refuses_an_absurd_number_at_once(self, runner):
        many = ",".join(f"ch001_{i:04d}" for i in range(300))
        with pytest.raises(JobError, match="too many"):
            runner.validate("resynth", {"slug": "solaris", "chunks": many})

    def test_it_locks_the_book(self):
        # It writes into the same rendered.jsonl a full render would.
        assert ACTIONS["resynth"]["locks"] is True
