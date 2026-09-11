"""Storage behavior across imports, independent narrations and restored roots."""
from __future__ import annotations

import json
import shutil
import sqlite3
import threading
import wave
from pathlib import Path

import pytest

from studio.catalog import Catalog, encode, inside
from studio.database import (
    Database,
    StorageError,
    journal_warning,
    migrate_queue,
    wal_fix_present,
)
from studio.imports import import_sources, import_folder
from studio.queue import Queue, SCHEMA as LEGACY_SCHEMA
from studio.runs import execute_stage, prepare_run
from studio.storage_backup import backup, restore


@pytest.fixture
def library(project):
    config = Path(__file__).resolve().parents[3] / "config"
    for name in ("pipeline.toml", "cast.yml"):
        shutil.copy2(config / name, project / "config" / name)
    (project / "config/cast.yml").write_text("roles:\n  narrator:\n    voice: michal\n")
    reference = project / "data/datasets/michal/wavs/seg_0000.wav"
    reference.parent.mkdir(parents=True)
    with wave.open(str(reference), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(b"\x00\x00" * 2400)
    return project


def imported(root: Path, text: str = "Zażółć gęślą jaźń. Ocean falował spokojnie.", **kwargs) -> str:
    source = root / "novel.txt"
    source.write_text(text, encoding="utf-8")
    result = import_sources(root, [source], slug="novel", language="pl", **kwargs)
    assert result["imported"] == 1, result
    return "novel"


def test_schema_constraints_and_runtime_mode(project):
    db = Database(project)
    assert db.check()["ok"]
    # WAL unconditionally: the queue is built on readers not blocking the
    # writer, and rollback journaling trades a rare race for constant
    # contention. An unfixed runtime is named, not worked around.
    report = db.check()
    assert report["journal_mode"] == "wal"
    assert report["wal_fix_present"] == wal_fix_present(sqlite3.sqlite_version_info)
    assert bool(report["warning"]) is not report["wal_fix_present"]
    with db.write() as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO book_revisions VALUES ('bad','missing',NULL,'fp','','','now')")
    assert Database(project).check()["schema_version"] == 1


def test_future_database_refused(project):
    db = Database(project)
    with db.write() as conn:
        conn.execute("PRAGMA user_version=999")
    with pytest.raises(StorageError, match="newer"):
        Database(project)


def test_migration_preserves_legacy_queue(project):
    old = project / "data/.studio/queue.db"
    old.parent.mkdir()
    with sqlite3.connect(old) as conn:
        conn.executescript(LEGACY_SCHEMA)
        conn.execute("INSERT INTO items(slug,action,position,created_at,updated_at) VALUES ('solaris','synth',0,'a','b')")
    with pytest.raises(StorageError, match="migration"):
        Database(project)
    assert migrate_queue(project).check()["ok"]
    assert Queue(project).items()[0].slug == "solaris"
    assert old.exists()
    assert (old.parent / "queue-before-catalog.db").exists()
    migrate_queue(project)
    assert len(Queue(project).items()) == 1


def test_active_legacy_queue_refuses_migration(project):
    old = project / "data/.studio/queue.db"
    old.parent.mkdir()
    with sqlite3.connect(old) as conn:
        conn.executescript(LEGACY_SCHEMA)
        conn.execute("INSERT INTO items(slug,action,position,status,created_at,updated_at) VALUES ('solaris','synth',0,'claimed','a','b')")
    with pytest.raises(StorageError, match="settle"):
        migrate_queue(project)
    assert not (project / "data/audiobook.db").exists()


def test_reimport_preserves_bytes_and_book_identity(library):
    imported(library)
    catalog = Catalog(library)
    first = catalog.books()[0]
    old_revision = catalog.one("SELECT * FROM book_revisions WHERE book_id=?", (first["id"],))
    old_bytes = catalog.asset_path(old_revision["source_asset_id"]).read_bytes()
    imported(library, "Nowa treść. Zupełnie inna książka.")
    second = catalog.books()[0]
    assert first["id"] == second["id"]
    assert first["current_text_id"] != second["current_text_id"]
    assert len(catalog.rows("SELECT * FROM book_revisions")) == 2
    assert catalog.asset_path(old_revision["source_asset_id"]).read_bytes() == old_bytes
    assert catalog.db.check()["ok"]


def test_duplicate_bytes_do_not_create_second_book(library):
    imported(library)
    other = library / "renamed.txt"
    shutil.copy2(library / "novel.txt", other)
    result = import_sources(library, [other])
    assert result["items"][0]["status"] == "unchanged"
    assert len(Catalog(library).books()) == 1


def test_encoding_decision_is_preserved(library):
    source = library / "polish.txt"
    source.write_bytes("Zażółć gęślą jaźń.".encode("cp1250"))
    result = import_sources(library, [source], encoding="cp1250", language="pl")
    assert result["imported"] == 1
    row = Catalog(library).one("SELECT * FROM text_versions")
    assert json.loads(row["encoding_json"])["encoding"] in ("cp1250", "windows-1250")
    assert "Zażółć" in row["snapshot_json"]


def test_bad_import_is_durable_and_other_books_continue(library):
    folder = library / "incoming"
    folder.mkdir()
    (folder / "broken.epub").write_bytes(b"not an epub")
    (folder / "valid.txt").write_text("The ocean was quiet. A traveller walked across the old bridge.")
    (folder / "ignored.pdf").write_bytes(b"pdf")
    result = import_folder(library, folder, language="en")
    assert result["imported"] == 1
    assert result["paused"] == 1
    statuses = {row["status"] for row in Catalog(library).rows("SELECT * FROM import_items")}
    assert statuses == {"imported", "failed", "unsupported"}


def test_registration_is_idempotent_and_legacy_unknowns_remain(project):
    catalog = Catalog(project)
    first = catalog.register_book("solaris")
    second = catalog.register_book("solaris")
    assert first == second
    assert catalog.one("SELECT source_asset_id FROM book_revisions")["source_asset_id"] is None
    assert json.loads(catalog.one("SELECT snapshot_json FROM model_snapshots")["snapshot_json"]) == {}


def test_reference_byte_change_creates_voice_revision(library):
    catalog = Catalog(library)
    first = catalog.register_voice("michal")
    path = library / "data/datasets/michal/wavs/seg_0000.wav"
    original = path.read_bytes()
    path.write_bytes(original + b"changed")
    second = catalog.register_voice("michal")
    assert first != second
    reference = catalog.one("SELECT * FROM voice_references WHERE revision_id=? AND kind='reference'", (first,))
    assert catalog.asset_path(reference["asset_id"]).read_bytes() == original


def test_run_freezes_text_configuration_and_voice(library):
    slug = imported(library)
    run = prepare_run(library, slug, voice="michal")
    base = inside(library, run["root_key"])
    configuration = (base / "config/models.toml").read_bytes()
    reference = (base / "data/datasets/michal/wavs/seg_0000.wav").read_bytes()
    (library / "config/models.toml").write_text("changed defaults")
    (library / "data/datasets/michal/wavs/seg_0000.wav").write_bytes(b"changed")
    imported(library, "Inna wersja książki.")
    assert (base / "config/models.toml").read_bytes() == configuration
    assert (base / "data/datasets/michal/wavs/seg_0000.wav").read_bytes() == reference
    assert "Zażółć" in (base / "data/book/novel/chapters.json").read_text()
    assert Catalog(library).run(run["id"])["text_version_id"] == run["text_version_id"]


def test_run_submission_is_idempotent_and_renditions_coexist(library):
    slug = imported(library)
    one = prepare_run(library, slug, voice="michal", request_key="request-a")
    again = prepare_run(library, slug, voice="michal", request_key="request-a")
    two = prepare_run(library, slug, voice="michal", request_key="request-b")
    assert one["id"] == again["id"] != two["id"]
    assert one["root_key"] != two["root_key"]
    assert len(Catalog(library).runs(slug)) == 2


def test_concurrent_submission_adds_one_plan(library):
    run = prepare_run(library, imported(library), voice="michal")
    errors = []
    def submit():
        try:
            Queue(library).add_plan("novel", [("chunk", {}), ("synth", {})], run_id=run["id"])
        except Exception as exc:
            errors.append(exc)
    threads = [threading.Thread(target=submit) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert len(Queue(library).items()) == 2


def test_chunk_dryrun_assembly_and_restore(library):
    slug = imported(library)
    run = prepare_run(library, slug, voice="michal")
    assert execute_stage(library, run["id"], "chunk") == 0
    assert execute_stage(library, run["id"], "dryrun") == 0
    assert execute_stage(library, run["id"], "assemble", fmt="wav") == 0
    catalog = Catalog(library)
    assert catalog.run(run["id"])["status"] == "done"
    assert catalog.rows("SELECT * FROM render_fragments")
    assert catalog.rows("SELECT * FROM exports")
    destination = library.parent / (library.name + "-backup")
    restored = library.parent / (library.name + "-restored")
    assert backup(library, destination)["ok"]
    assert restore(destination, restored)["ok"]
    recovered = Catalog(restored)
    assert recovered.run(run["id"])["status"] == "done"
    assert (restored / run["root_key"] / "data/out/novel.wav").is_file()
    assert execute_stage(restored, run["id"], "assemble", fmt="wav") == 0


def test_restore_rejects_tampered_asset(library):
    imported(library)
    destination = library.parent / (library.name + "-backup")
    backup(library, destination)
    asset = Catalog(destination).one("SELECT * FROM assets")
    (destination / asset["storage_key"]).write_bytes(b"corrupt")
    target = library.parent / (library.name + "-restore")
    with pytest.raises(StorageError, match="checksum"):
        restore(destination, target)
    assert not target.exists()


def test_review_survives_restart_and_is_visible(library, monkeypatch):
    from bookbinder.language import LanguageDecision
    from studio.data import get_book
    monkeypatch.setattr("bookbinder.language.decide", lambda *a, **kw:
                        LanguageDecision(needs_review=True, review_reasons=["Choose a language"]))
    source = library / "uncertain.txt"
    source.write_text("A short passage.")
    result = import_sources(library, [source])
    assert result["items"][0]["status"] == "review"
    book = get_book(library, "uncertain")
    assert book and book.needs_review and book.language == "und"
    with pytest.raises(StorageError, match="encoding and language"):
        prepare_run(library, "uncertain", voice="michal")


def test_changed_text_does_not_reuse_old_plan(library):
    slug = imported(library)
    from studio.catalog_cli import app
    from typer.testing import CliRunner
    result = CliRunner().invoke(app, ["process", slug, "chunk", "--voice", "michal"])
    assert result.exit_code == 0, result.output
    old = Catalog(library).books()[0]["current_plan_id"]
    assert old
    imported(library, "Całkiem inna treść. Musi powstać nowy podział.")
    run = prepare_run(library, slug, voice="michal")
    assert run["plan_id"] is None
    assert execute_stage(library, run["id"], "chunk") == 0
    assert Catalog(library).run(run["id"])["plan_id"] != old


def test_frozen_records_cannot_be_updated(library):
    run = prepare_run(library, imported(library), voice="michal")
    with Catalog(library).db.write() as conn, pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("UPDATE audiobook_runs SET snapshot_json='{}' WHERE id=?", (run["id"],))
    with Catalog(library).db.write() as conn, pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("UPDATE text_versions SET language='en'")


def test_each_language_can_choose_a_different_model(library):
    registry = library / "config/models.toml"
    text = registry.read_text()
    extra = "[models.english-candidate]" + text.split("[models.xtts-v2]", 1)[1].replace("[models.xtts-v2.", "[models.english-candidate.")
    registry.write_text(text.replace('en = "xtts-v2"', 'en = "english-candidate"') + "\n" + extra)
    polish = prepare_run(library, imported(library), voice="michal")
    source = library / "english.txt"
    source.write_text("The traveller walked quietly beside the ocean.")
    assert import_sources(library, [source], language="en")["imported"] == 1
    english = prepare_run(library, "english", voice="michal")
    assert json.loads(polish["snapshot_json"])["model"]["id"] == "xtts-v2"
    assert json.loads(english["snapshot_json"])["model"]["id"] == "english-candidate"


def test_worker_reconciles_completed_attempt_without_job_file(library):
    from studio.worker import Worker
    from studio.database import now
    run = prepare_run(library, imported(library), voice="michal")
    queue = Queue(library)
    queue.add_plan("novel", [("chunk", {})], run_id=run["id"])
    item = queue.claim("worker-that-disappeared")
    assert item
    with Catalog(library).db.write() as conn:
        conn.execute("INSERT INTO job_attempts(id,run_id,queue_id,stage,status,started_at) VALUES (?,?,?,?,?,?)",
                     ("attempt", run["id"], item.id, "chunk", "done", now()))
    assert Worker(library).settle() == 1
    assert queue.require(item.id).status == "done"


def test_worker_runs_a_real_chunk_job(library):
    import time
    from studio.worker import Worker
    run = prepare_run(library, imported(library), voice="michal")
    Queue(library).add_plan("novel", [("chunk", {})], run_id=run["id"])
    worker = Worker(library)
    assert worker.step()[0] == "novel/chunk"
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        worker.settle()
        item = worker.queue.items()[0]
        if item.status in ("done", "failed"):
            assert item.status == "done", worker.runner.tail(item.job_id)
            break
        time.sleep(0.1)
    else:
        worker.runner.cancel(worker.queue.items()[0].job_id)
        pytest.fail("chunk worker did not finish")
    assert Catalog(library).run(run["id"])["plan_id"]


def test_partial_ledger_is_registered_and_replaced_audio_is_preserved(library):
    from bookbinder.fingerprint import fragment_fingerprint, voice_revision
    from bookbinder.manifest import ModelChoice, BookMeta
    from bookbinder.assemble import stale_fragments
    run = prepare_run(library, imported(library), voice="michal")
    assert execute_stage(library, run["id"], "chunk") == 0
    catalog = Catalog(library)
    run = catalog.run(run["id"])
    base = inside(library, run["root_key"])
    snapshot = json.loads(run["snapshot_json"])
    chunk = json.loads(catalog.one("SELECT snapshot_json FROM chunks WHERE plan_id=? ORDER BY position LIMIT 1", (run["plan_id"],))["snapshot_json"])
    model = ModelChoice.model_validate(snapshot["model"])
    settings = dict(model.settings) | snapshot["cast_settings"].get(chunk["role"], {})
    fp = fragment_fingerprint(text=chunk["text"], language=chunk["language"], model=model.identity,
                              voice="michal", voice_revision=voice_revision(base, "michal"), settings=settings)
    audio = base / "data/audio/novel"
    audio.mkdir(parents=True)
    wav = audio / f"{chunk['id']}.wav"
    shutil.copy2(base / "data/datasets/michal/wavs/seg_0000.wav", wav)
    ledger = audio / "fingerprints.jsonl"
    ledger.write_text(encode({"id": chunk["id"], "fingerprint": fp}) + '\n{"torn":')
    assert catalog.register_results(run["id"])["fragments"] == 1
    first = catalog.one("SELECT * FROM render_fragments")
    with wave.open(str(wav), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(24000)
        handle.writeframes(b"\x01\x01" * 2400)
    catalog.register_results(run["id"])
    second = catalog.one("SELECT * FROM render_fragments")
    assert first["asset_id"] != second["asset_id"]
    assert catalog.asset_path(first["asset_id"]).exists()
    meta = BookMeta.model_validate_json((base / "data/book/novel/book.json").read_text())
    assert not stale_fragments(base, [json.loads(second["snapshot_json"])], {chunk["id"]: chunk}, meta)
    ledger.write_text(encode({"id": chunk["id"], "fingerprint": "wrong"}))
    with pytest.raises(StorageError, match="approved inputs"):
        catalog.register_results(run["id"])


def test_stale_claim_cannot_change_another_workers_item(project):
    from studio.queue import QueueError
    queue = Queue(project)
    item = queue.add("solaris", "chunk")
    first = queue.claim("one")
    assert first
    with Database(project).write() as conn:
        conn.execute("UPDATE items SET claimed_at='2000-01-01T00:00:00+00:00' WHERE id=?", (item.id,))
    second = queue.claim("two")
    assert second
    for action in (lambda: queue.start(item.id, "job", claim=first.claim),
                   lambda: queue.release(item.id, claim=first.claim),
                   lambda: queue.finish(item.id, True, claim=first.claim)):
        with pytest.raises(QueueError):
            action()
    assert queue.require(item.id).claim == "two"


def test_missing_legacy_source_does_not_drop_book(project):
    folder = project / "data/book/solaris"
    meta = json.loads((folder / "book.json").read_text())
    meta["source_file"] = "/old/machine/data/raw/books/missing.txt"
    (folder / "book.json").write_text(encode(meta))
    catalog = Catalog(project)
    catalog.register_book("solaris")
    assert len(catalog.books()) == 1
    assert catalog.one("SELECT source_asset_id FROM book_revisions")["source_asset_id"] is None


def test_noop_chunk_records_a_completed_attempt(library):
    slug = imported(library)
    run = prepare_run(library, slug, voice="michal")
    assert execute_stage(library, run["id"], "chunk") == 0
    assert execute_stage(library, run["id"], "chunk") == 0
    attempts = Catalog(library).rows("SELECT status FROM job_attempts WHERE run_id=?", (run["id"],))
    assert [a["status"] for a in attempts] == ["done", "done"]


def test_request_key_cannot_return_another_book(library):
    slug = imported(library)
    prepare_run(library, slug, voice="michal", request_key="submission")
    with pytest.raises(StorageError, match="another book"):
        prepare_run(library, "another-book", request_key="submission")


def test_backup_restore_through_symlinked_parent(library, tmp_path):
    imported(library)
    link = tmp_path / "alias"
    actual = tmp_path / "physical"
    actual.mkdir()
    link.symlink_to(actual, target_is_directory=True)
    assert backup(library, link / "backup")["ok"]
    assert restore(link / "backup", link / "restored")["ok"]
    assert len(Catalog(actual / "restored").books()) == 1


class TestOneCopyOfEveryFragment:
    """A run's audio and the stored asset are one file under two names.

    Copying meant a second set of bytes for every fragment ever rendered. A
    twenty-hour book is several gigabytes of them, and comparing two voices for
    the same book doubles it again, so this is most of the library's size.
    """

    def _rendered(self, library):
        slug = imported(library)
        run = prepare_run(library, slug, voice="michal")
        assert execute_stage(library, run["id"], "chunk") == 0
        assert execute_stage(library, run["id"], "dryrun") == 0
        assert execute_stage(library, run["id"], "assemble", fmt="wav") == 0
        return Catalog(library), run

    def test_a_fragment_shares_its_inode_with_the_asset(self, library):
        catalog, run = self._rendered(library)
        row = catalog.one("SELECT * FROM render_fragments LIMIT 1")
        stored = catalog.asset_path(row["asset_id"])
        base = inside(library, catalog.run(run["id"])["root_key"])
        fragment = next(iter(sorted(base.glob("data/audio/*/*.wav"))))
        assert stored.stat().st_ino == fragment.stat().st_ino

    def test_the_export_shares_its_inode_too(self, library):
        catalog, run = self._rendered(library)
        row = catalog.one("SELECT * FROM exports LIMIT 1")
        stored = catalog.asset_path(row["asset_id"])
        assert stored.stat().st_nlink > 1

    def test_a_rendered_fragment_costs_its_bytes_once(self, library):
        catalog, run = self._rendered(library)
        base = inside(library, catalog.run(run["id"])["root_key"])
        seen, bytes_on_disk = set(), 0
        for path in sorted(base.glob("data/audio/*/*.wav")):
            info = path.stat()
            if info.st_ino not in seen:
                seen.add(info.st_ino)
                bytes_on_disk += info.st_size
        stored = sum(p.stat().st_size for p in (library / "data/assets").rglob("*.wav")
                     if p.stat().st_ino in seen)
        assert stored == bytes_on_disk


class TestWhatMustNotBeCollapsed:
    """Linking is only safe where the writer publishes by rename.

    A voice reference is a file a person may open and edit. Folding it into the
    asset store means editing it rewrites a file whose name is the hash of what
    it used to contain, and the catalog then disagrees with itself. This was
    not hypothetical: the first attempt did exactly that.
    """

    def test_editing_a_voice_reference_leaves_the_asset_alone(self, library):
        catalog = Catalog(library)
        first = catalog.register_voice("michal")
        path = library / "data/datasets/michal/wavs/seg_0000.wav"
        original = path.read_bytes()

        path.write_bytes(original + b"changed")

        reference = catalog.one(
            "SELECT * FROM voice_references WHERE revision_id=? AND kind='reference'",
            (first,))
        assert catalog.asset_path(reference["asset_id"]).read_bytes() == original

    def test_the_imported_book_keeps_its_own_bytes(self, library):
        # A book still in someone's Downloads folder is theirs; the catalog has
        # no business rewriting it into a link to its own store.
        catalog = Catalog(library)
        source = library / "data/raw/books/probe.txt"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("Ocean falowal pod stacja." * 40, encoding="utf-8")
        stored = catalog.asset_path(catalog.asset(source))
        assert stored.stat().st_ino != source.stat().st_ino


class TestTheJournalIsReconciledOnOpen:
    def test_a_database_left_in_rollback_mode_is_corrected(self, library):
        import sqlite3 as sqlite

        db = Database(library)
        with db.connect() as conn:
            conn.execute("PRAGMA journal_mode=DELETE")
        raw = sqlite.connect(db.path)
        assert raw.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        raw.close()

        assert Database(library).check()["journal_mode"] == "wal"

