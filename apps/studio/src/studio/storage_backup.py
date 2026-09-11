"""A restorable catalog snapshot includes every referenced immutable asset."""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
from pathlib import Path

from bookbinder.manifest import publish_text
from studio.catalog import Catalog, encode, inside
from studio.database import Database, StorageError, database_path


def file_hash(path: Path) -> str:
    hashed = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            hashed.update(block)
    return hashed.hexdigest()


def materialize_run(catalog: Catalog, run_id: str) -> None:
    run = catalog.run(run_id)
    snapshot = json.loads(run["snapshot_json"])
    root = inside(catalog.root, run["root_key"])
    text = catalog.one("SELECT * FROM text_versions WHERE id=?", (run["text_version_id"],))
    payload = json.loads(text["snapshot_json"])
    revision = catalog.one("SELECT * FROM book_revisions WHERE id=?", (text["revision_id"],))
    if revision["source_asset_id"]:
        payload["meta"]["source_file"] = catalog.asset_path(revision["source_asset_id"]).relative_to(catalog.root).as_posix()
    book_dir = root / "data/book" / run["slug"]
    book_dir.mkdir(parents=True, exist_ok=True)
    publish_text(book_dir / "chapters.json", encode(payload))
    for name, content in snapshot["configuration"].items():
        target = inside(root, f"config/{name}")
        target.parent.mkdir(parents=True, exist_ok=True)
        publish_text(target, content)
    (root / "config").mkdir(exist_ok=True)
    for name, content in snapshot.get("overrides", {}).items():
        publish_text(inside(book_dir, name), content)
    revision = catalog.one("SELECT * FROM book_revisions WHERE id=?", (text["revision_id"],))
    source_file = payload["meta"].get("source_file")
    if revision["source_asset_id"] and source_file:
        target = inside(root, source_file)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(catalog.asset_path(revision["source_asset_id"]), target)
    if run["plan_id"]:
        plan = catalog.one("SELECT * FROM chunk_plans WHERE id=?", (run["plan_id"],))
        publish_text(book_dir / "book.json", plan["meta_json"])
        chunks = catalog.rows("SELECT * FROM chunks WHERE plan_id=? ORDER BY position", (run["plan_id"],))
        publish_text(book_dir / "chunks.jsonl", "".join(row["snapshot_json"] + "\n" for row in chunks))
    for name, rev_id in snapshot["voice_revisions"].items():
        voice = catalog.one("SELECT profile_json FROM voice_revisions WHERE id=?", (rev_id,))
        target = inside(root, f"data/voices/{name}.json")
        target.parent.mkdir(parents=True, exist_ok=True)
        publish_text(target, voice["profile_json"])
        for ref in catalog.rows("SELECT * FROM voice_references WHERE revision_id=?", (rev_id,)):
            if ref["asset_id"]:
                target = inside(root, ref["path"])
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(catalog.asset_path(ref["asset_id"]), target)
    audio = root / "data/audio" / run["slug"]
    audio.mkdir(parents=True, exist_ok=True)
    fragments = catalog.rows("SELECT f.* FROM render_fragments f JOIN chunks c ON c.id=f.chunk_id "
                             "WHERE f.run_id=? ORDER BY c.position", (run_id,))
    if fragments:
        fingerprints = []
        for fragment in fragments:
            row = json.loads(fragment["snapshot_json"])
            target = inside(root, row["audio_path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(catalog.asset_path(fragment["asset_id"]), target)
            fingerprints.append(encode({"id": row["id"], "fingerprint": fragment["fingerprint"]}))
        publish_text(audio / "rendered.jsonl", "".join(f["snapshot_json"] + "\n" for f in fragments))
        publish_text(audio / "fingerprints.jsonl", "\n".join(fingerprints) + "\n")
    for report in catalog.rows("SELECT * FROM run_reports WHERE run_id=?", (run_id,)):
        content = json.loads(report["content_json"])
        if report["name"] == "progress.json":
            content["running"] = False
        publish_text(inside(audio, report["name"]), encode(content))
    out = root / "data/out"
    out.mkdir(parents=True, exist_ok=True)
    for export in catalog.rows("SELECT * FROM exports WHERE run_id=? ORDER BY created_at", (run_id,)):
        shutil.copy2(catalog.asset_path(export["asset_id"]), out / f"{run['slug']}.{export['format']}")
        metadata = json.loads(export["metadata_json"])
        publish_text(out / f"{run['slug']}.chapters.json", encode(metadata.get("chapters", []) if isinstance(metadata, dict) else metadata))


def backup(root: Path, destination: Path) -> dict:
    root, destination = root.resolve(), destination.resolve()
    if destination.exists():
        raise StorageError("backup destination already exists; choose a new directory")
    db = Database(root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staged = Path(tempfile.mkdtemp(prefix=".backup-", dir=destination.parent))
    try:
        snapshot = staged / "data/audiobook.db"
        snapshot.parent.mkdir()
        with db.connect() as source:
            target = sqlite3.connect(snapshot)
            try:
                source.backup(target)
            finally:
                target.close()
        copied = Catalog(staged)
        for asset in copied.rows("SELECT * FROM assets"):
            source_file = inside(root, asset["storage_key"])
            target_file = inside(staged, asset["storage_key"])
            target_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_file, target_file)
            if file_hash(target_file) != asset["sha256"]:
                raise StorageError(f"asset checksum mismatch: {asset['id']}")
        if (root / "config").is_dir():
            shutil.copytree(root / "config", staged / "config")
        check = copied.db.check()
        if not check["ok"]:
            raise StorageError(f"backup integrity failed: {check}")
        (staged / "backup.json").write_text(encode({"format": 1, "assets": len(copied.rows('SELECT id FROM assets'))}))
        staged.rename(destination)
        return check
    except BaseException:
        shutil.rmtree(staged, ignore_errors=True)
        raise


def restore(source: Path, destination: Path) -> dict:
    source, destination = source.resolve(), destination.resolve()
    if destination.exists():
        raise StorageError("restore requires a new destination directory")
    if not (source / "backup.json").is_file() or not database_path(source).is_file():
        raise StorageError("not a catalog backup")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staged = Path(tempfile.mkdtemp(prefix=".restore-", dir=destination.parent))
    try:
        shutil.copytree(source, staged, dirs_exist_ok=True)
        catalog = Catalog(staged)
        check = catalog.db.check()
        if not check["ok"]:
            raise StorageError("backup database integrity failed")
        for asset in catalog.rows("SELECT * FROM assets"):
            if file_hash(inside(staged, asset["storage_key"])) != asset["sha256"]:
                raise StorageError(f"asset checksum mismatch: {asset['id']}")
        with catalog.db.write() as conn:
            conn.execute("UPDATE items SET status='pending',claim='',claimed_at='',job_id='' WHERE status IN ('claimed','running')")
            conn.execute("UPDATE job_attempts SET status='interrupted',error='restored from backup' WHERE status IN ('running','launching')")
            conn.execute("UPDATE audiobook_runs SET status='pending' WHERE status='running'")
        for book in catalog.books():
            text = catalog.one("SELECT * FROM text_versions WHERE id=?", (book["current_text_id"],))
            folder = staged / "data/book" / book["slug"]
            folder.mkdir(parents=True, exist_ok=True)
            payload = json.loads(text["snapshot_json"])
            revision = catalog.one("SELECT * FROM book_revisions WHERE id=?", (text["revision_id"],))
            if revision["source_asset_id"]:
                source = catalog.asset_path(revision["source_asset_id"])
                payload["meta"]["source_file"] = source.relative_to(staged).as_posix()
            publish_text(folder / "chapters.json", encode(payload))
            if book["current_plan_id"]:
                plan = catalog.one("SELECT * FROM chunk_plans WHERE id=?", (book["current_plan_id"],))
                publish_text(folder / "book.json", plan["meta_json"])
                chunks = catalog.rows("SELECT snapshot_json FROM chunks WHERE plan_id=? ORDER BY position", (plan["id"],))
                publish_text(folder / "chunks.jsonl", "".join(c["snapshot_json"] + "\n" for c in chunks))
        for voice in catalog.rows("SELECT * FROM voices"):
            rev = catalog.one("SELECT * FROM voice_revisions WHERE voice_id=? ORDER BY created_at DESC LIMIT 1", (voice["id"],))
            profile = staged / "data/voices" / f"{voice['name']}.json"
            profile.parent.mkdir(parents=True, exist_ok=True)
            publish_text(profile, rev["profile_json"])
            for ref in catalog.rows("SELECT * FROM voice_references WHERE revision_id=?", (rev["id"],)):
                if ref["asset_id"]:
                    target = inside(staged, ref["path"])
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(catalog.asset_path(ref["asset_id"]), target)
        for run in catalog.runs():
            materialize_run(catalog, run["id"])
        staged.rename(destination)
        return check
    except BaseException:
        shutil.rmtree(staged, ignore_errors=True)
        raise
