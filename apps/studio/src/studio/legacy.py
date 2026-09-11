"""Archive existing audio without inventing its missing historical settings."""
from __future__ import annotations

import shutil

from studio.catalog import Catalog, digest, encode, identity, read_json, read_lines
from studio.database import StorageError, now


def archive_audio(catalog: Catalog, slug: str) -> str | None:
    root = catalog.root
    if (root / "data/book" / slug / ".needs-chunking").exists():
        return None
    audio = root / "data/audio" / slug
    rendered = read_lines(audio / "rendered.jsonl")
    outputs = [p for p in (root / "data/out").glob(f"{slug}.*") if p.suffix in (".wav", ".mp3", ".m4b")]
    if not rendered and not outputs:
        return None
    source_assets = {}
    for row in rendered:
        if row.get("audio_path"):
            source_assets[row["audio_path"]] = catalog.asset(root / row["audio_path"])
    for path in outputs:
        source_assets[path.relative_to(root).as_posix()] = catalog.asset(path)
    key = "legacy:" + digest({"slug": slug, "rendered": rendered, "assets": source_assets})
    existing = catalog.rows("SELECT id FROM audiobook_runs WHERE request_key=?", (key,))
    if existing:
        return existing[0]["id"]
    book = catalog.one("SELECT * FROM books WHERE slug=?", (slug,))
    planned = {row["chunk_key"]: row for row in catalog.rows("SELECT * FROM chunks WHERE plan_id=?", (book["current_plan_id"],))}
    if any(row["id"] not in planned or row.get("text") != planned[row["id"]]["spoken_text"] for row in rendered):
        raise StorageError(f"{slug}: legacy audio does not match the current text; original files retained for review")
    meta = read_json(root / "data/book" / slug / "book.json", {})
    snapshot = {"legacy": True, "model": meta.get("model") or {}, "voice": meta.get("voice") or "",
                "cast": meta.get("cast") or {}, "cast_settings": meta.get("cast_settings") or {},
                "configuration": {}, "overrides": {}, "voice_revisions": {}}
    run_id = identity()
    run_key = f"data/runs/{run_id}"
    base = root / run_key
    (base / "data/book").mkdir(parents=True)
    shutil.copytree(root / "data/book" / slug, base / "data/book" / slug)
    for path_key, asset in source_assets.items():
        target = base / path_key
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(catalog.asset_path(asset), target)
    for filename in ("rendered.jsonl", "report.json", "progress.json", "qa_report.json", ".dry-run.json"):
        if (audio / filename).is_file():
            target = base / "data/audio" / slug / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(audio / filename, target)
    chapters = root / "data/out" / f"{slug}.chapters.json"
    if chapters.is_file():
        (base / "data/out").mkdir(parents=True, exist_ok=True)
        shutil.copy2(chapters, base / "data/out" / chapters.name)
    model_id = catalog.model(snapshot["model"])
    with catalog.db.write() as conn:
        conn.execute("INSERT INTO audiobook_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (run_id, book["id"], book["current_text_id"], book["current_plan_id"], model_id,
                      key, run_key, "legacy", encode(snapshot), now(), now(), "historical model/voice provenance may be unavailable"))
    catalog.register_results(run_id)
    return run_id
