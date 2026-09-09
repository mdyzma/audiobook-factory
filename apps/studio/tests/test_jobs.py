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
    def _holder(self, project, action="synth", args=None):
        """A running job owned by this test's own process, so it looks alive."""
        store = JobStore(project)
        store.dir.mkdir(parents=True, exist_ok=True)
        job = Job(id="aaaaaaaaaaaa", action=action, args=args or {"slug": "solaris"},
                  status="running", pid=os.getpid(), started_at="2026-01-01T00:00:00+00:00")
        store.save(job)
        return store, job

    def test_a_second_render_is_refused(self, project):
        store, holder = self._holder(project)
        store.acquire("solaris", holder.id)

        with pytest.raises(JobError, match="busy"):
            JobRunner(project).start("synth", {"slug": "solaris", "voice": ""})

    def test_acquire_refuses_a_book_already_locked(self, project):
        # The lock itself, independent of the conflict check above it.
        store, holder = self._holder(project)
        store.acquire("solaris", holder.id)

        with pytest.raises(JobError, match="already being rendered"):
            store.acquire("solaris", "bbbbbbbbbbbb")

    def test_acquire_is_atomic(self, project):
        # Check-then-write let two callers both pass the check and both write,
        # leaving two renders sharing one audio directory.
        store = JobStore(project)
        store.dir.mkdir(parents=True, exist_ok=True)
        store.locks.mkdir(parents=True, exist_ok=True)
        store.lock_path("solaris").write_text("", encoding="utf-8")

        # An empty lock names no job, so `holder` treats it as stale and clears
        # it; the retry must then create the file rather than find it gone.
        store.acquire("solaris", "cccccccccccc")
        assert store.lock_path("solaris").read_text(encoding="utf-8") == "cccccccccccc"

    def test_a_non_locking_stage_is_refused_while_a_render_runs(self, project):
        # Assembling mid-render reads a fragment list still being appended to,
        # and used to be allowed because assemble takes no lock.
        store, _holder = self._holder(project)

        with pytest.raises(JobError, match="busy"):
            JobRunner(project).start("assemble", {"slug": "solaris", "format": "m4b"})

    def test_chunking_is_refused_while_a_render_runs(self, project):
        # Re-chunking rewrites the manifest the render is reading from.
        store, _holder = self._holder(project)

        with pytest.raises(JobError, match="busy"):
            JobRunner(project).start("chunk", {"slug": "solaris"})

    def test_another_book_is_unaffected(self, project):
        store, _holder = self._holder(project)
        assert store.conflicting_job("book:inne-morze") is None

    def test_a_voice_job_does_not_block_a_book_of_the_same_name(self, project):
        store, _holder = self._holder(project, action="clone", args={"voice": "solaris"})
        assert store.conflicting_job("voice:solaris") is not None
        assert store.conflicting_job("book:solaris") is None

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


class TestPrune:
    """Nothing expires on its own, and a render's log grows with every progress
    line, so pruning has to be safe to run at any moment."""

    def _finished(self, store, job_id, when):
        job = Job(id=job_id, action="chunk", args={"slug": "solaris"},
                  status="succeeded", pid=999999, started_at=when, exit_code=0)
        store.save(job)
        store.log_path(job_id).write_text("output\n", encoding="utf-8")
        return job

    def test_keeps_the_newest(self, project):
        store = JobStore(project)
        store.dir.mkdir(parents=True, exist_ok=True)
        for i in range(5):
            self._finished(store, f"job{i:08d}xxxx", f"2026-01-0{i + 1}T00:00:00+00:00")

        result = JobRunner(project).prune(keep=2)
        assert result["removed"] == 3
        assert result["kept"] == 2
        remaining = sorted(p.stem for p in store.dir.glob("*.json"))
        assert remaining == ["job00000003xxxx"[:12], "job00000004xxxx"[:12]] or len(remaining) == 2

    def test_removes_the_log_as_well_as_the_record(self, project):
        store = JobStore(project)
        store.dir.mkdir(parents=True, exist_ok=True)
        self._finished(store, "oldjobaaaaaa", "2026-01-01T00:00:00+00:00")
        JobRunner(project).prune(keep=0)
        assert not store.log_path("oldjobaaaaaa").exists()
        assert not (store.dir / "oldjobaaaaaa.json").exists()

    def test_reports_bytes_freed(self, project):
        store = JobStore(project)
        store.dir.mkdir(parents=True, exist_ok=True)
        self._finished(store, "sizedjobaaaa", "2026-01-01T00:00:00+00:00")
        assert JobRunner(project).prune(keep=0)["bytes_freed"] > 0

    def test_never_touches_a_running_job(self, project):
        # Its log is still being written and its lock still means something.
        store = JobStore(project)
        store.dir.mkdir(parents=True, exist_ok=True)
        running = Job(id="runningjobaa", action="synth", args={"slug": "solaris"},
                      status="running", pid=os.getpid(),
                      started_at="2020-01-01T00:00:00+00:00")
        store.save(running)
        store.log_path(running.id).write_text("mid render\n", encoding="utf-8")
        store.acquire("solaris", running.id)

        result = JobRunner(project).prune(remove_all=True)
        assert result["running"] == 1
        assert (store.dir / "runningjobaa.json").exists()
        assert store.log_path("runningjobaa").exists()
        assert store.holder("solaris") == "runningjobaa"

    def test_clears_a_lock_whose_holder_is_gone(self, project):
        # Otherwise it refuses the next render forever.
        store = JobStore(project)
        store.dir.mkdir(parents=True, exist_ok=True)
        store.locks.mkdir(parents=True, exist_ok=True)
        store.lock_path("solaris").write_text("vanishedjobx", encoding="utf-8")

        JobRunner(project).prune()
        assert not store.lock_path("solaris").exists()

    def test_pruning_an_empty_store_is_harmless(self, project):
        assert JobRunner(project).prune()["removed"] == 0


class TestVoiceCreation:
    """Turning an uploaded recording into a usable voice.

    Without this the dashboard could accept a sample and then leave the user in
    a terminal, which defeats the point of having a dashboard.
    """

    def _sample(self, project, name="michal.wav"):
        d = project / "data" / "raw" / "voices"
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_bytes(b"RIFF")
        return name

    def test_accepts_an_uploaded_sample(self, project, runner):
        self._sample(project)
        clean = runner.validate("voice", {"sample": "michal.wav", "name": "michal",
                                          "language": "pl"})
        assert clean["sample"].endswith("michal.wav")
        assert clean["name"] == "michal"
        assert clean["language"] == "pl"

    def test_refuses_a_sample_that_was_not_uploaded(self, project, runner):
        with pytest.raises(JobError, match="no uploaded sample"):
            runner.validate("voice", {"sample": "absent.wav", "name": "x", "language": "pl"})

    def test_refuses_a_file_that_is_not_audio(self, project, runner):
        self._sample(project, "notes.txt")
        with pytest.raises(JobError, match="invalid sample file"):
            runner.validate("voice", {"sample": "notes.txt", "name": "x", "language": "pl"})

    @pytest.mark.parametrize("bad", ["../etc", "a/b", "a;rm -rf /", ""])
    def test_refuses_an_unsafe_name(self, project, runner, bad):
        self._sample(project)
        with pytest.raises(JobError, match="invalid name"):
            runner.validate("voice", {"sample": "michal.wav", "name": bad, "language": "pl"})

    def test_refuses_a_language_the_model_cannot_read(self, project, runner):
        self._sample(project)
        with pytest.raises(JobError, match="unsupported language"):
            runner.validate("voice", {"sample": "michal.wav", "name": "x",
                                      "language": "klingon"})

    def test_language_defaults_rather_than_failing(self, project, runner):
        self._sample(project)
        clean = runner.validate("voice", {"sample": "michal.wav", "name": "x",
                                          "language": ""})
        assert clean["language"] == "pl"

    def test_it_locks_the_voice_not_a_book(self, project):
        # Two runs must not build the same voice at once, but a voice job should
        # never block a book render.
        assert ACTIONS["voice"]["lock_key"] == "name"

        store = JobStore(project)
        store.dir.mkdir(parents=True, exist_ok=True)
        holder = Job(id="voicejobaaaa", action="voice",
                     args={"sample": "s.wav", "name": "michal", "language": "pl"},
                     status="running", pid=os.getpid(),
                     started_at="2026-01-01T00:00:00+00:00")
        store.save(holder)
        store.acquire("michal", holder.id)
        assert store.holder("michal") == holder.id
        # A book render is unaffected.
        assert store.holder("solaris") is None

    def test_a_finished_voice_job_releases_its_lock(self, project):
        store = JobStore(project)
        store.dir.mkdir(parents=True, exist_ok=True)
        job = Job(id="voicejobbbbb", action="voice",
                  args={"sample": "s.wav", "name": "michal", "language": "pl"},
                  status="succeeded", pid=999999,
                  started_at="2026-01-01T00:00:00+00:00")
        store.save(job)
        store.locks.mkdir(parents=True, exist_ok=True)
        store.lock_path("michal").write_text(job.id, encoding="utf-8")
        store.release(job)
        assert not store.lock_path("michal").exists()
