"""Freeze inputs once; run the existing stages against a private data root."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from bookbinder.models import RegistryError, load_registry
from bookbinder.manifest import publish_text

from studio.catalog import (
    Catalog,
    digest,
    encode,
    identity,
    inside,
    place,
    read_json,
    read_lines,
)
from studio.database import StorageError, now

STAGES = ("chunk", "synth", "dryrun", "assemble", "verify")


def prepare_run(root: Path, slug: str, *, voice: str = "", model: str = "",
                request_key: str = "", reuse: bool = False, single_voice: bool = False) -> dict:
    catalog = Catalog(root)
    if request_key:
        existing = catalog.rows("SELECT r.id,b.slug FROM audiobook_runs r JOIN books b ON b.id=r.book_id WHERE request_key=?", (request_key,))
        if existing:
            if existing[0]["slug"] != slug:
                raise StorageError("this request key already belongs to another book")
            return catalog.run(existing[0]["id"])
    book = catalog.register_book(slug)
    text = catalog.one("SELECT * FROM text_versions WHERE id=?", (book["text_version_id"],))
    payload = json.loads(text["snapshot_json"])
    meta = payload["meta"]
    source_revision = catalog.one("SELECT * FROM book_revisions WHERE id=?", (text["revision_id"],))
    if source_revision["source_asset_id"]:
        meta["source_file"] = catalog.asset_path(source_revision["source_asset_id"]).relative_to(catalog.root).as_posix()
    if meta.get("needs_review") or not text["language"]:
        raise StorageError("resolve the book's encoding and language before preparing a run")
    existing_meta = read_json(root / "data/book" / slug / "book.json", {})
    current_model = existing_meta.get("model") or {}
    try:
        spec = load_registry(root).resolve(text["language"], override=model or current_model.get("id") or "")
    except RegistryError as exc:
        raise StorageError(str(exc)) from exc
    import tomllib
    pipeline = root / "config/pipeline.toml"
    config = tomllib.loads(pipeline.read_text()) if pipeline.is_file() else {}
    settings = {k: float(v) for k, v in config.get("synth", {}).items()
                if isinstance(v, (float, int)) and k != "retries"}
    # A recorded current plan has already resolved its own settings.
    selected = current_model if book["plan_id"] and current_model and not model else {
        "id": spec.id, "engine": spec.engine, "environment": spec.environment,
        "checkpoint": spec.checkpoint, "revision": spec.revision,
        "native_sample_rate": spec.native_sample_rate,
        "char_limit": min(spec.char_limit(text["language"]), config.get("chunk", {}).get("max_chars") or spec.char_limit(text["language"])),
        "settings": spec.supported_controls(settings), "unsupported": spec.unsupported_controls(settings),
        "source": "override" if model else "default"}
    cast = dict(existing_meta.get("cast") or {})
    chosen_voice = voice or existing_meta.get("voice") or ""
    if not cast:
        from studio.batch import default_cast
        cast = default_cast(root)
    if chosen_voice:
        cast = {role: chosen_voice for role in {*cast, "narrator"}}
    if single_voice:
        cast = {"narrator": chosen_voice or cast.get("narrator") or ""}
    if not cast:
        raise StorageError("choose a narrator voice before preparing an audiobook")
    revisions = {name: catalog.register_voice(name) for name in set(cast.values())}
    cast_settings = existing_meta.get("cast_settings") or {}
    if not cast_settings and (root / "config/cast.yml").is_file():
        from bookbinder.cast import Cast
        cast_settings = {role: spec.supported_controls({"speed": setting.speed})
                         for role, setting in Cast.load(root / "config/cast.yml").roles.items()}
    configuration = {p.name: p.read_text() for p in (root / "config").glob("*") if p.is_file()}
    overrides = {p.name: p.read_text() for p in (root / "data/book" / slug).glob("*")
                 if p.name in ("role_overrides.json", "pronunciation.yml")}
    snapshot = {"slug": slug, "text_version_id": text["id"], "model": selected,
                "voice": chosen_voice, "cast": cast, "cast_settings": cast_settings, "voice_revisions": revisions,
                "configuration": configuration, "overrides": overrides, "single_voice": single_voice}
    if single_voice:
        cast_settings = {}
        snapshot["cast_settings"] = cast_settings
    key = request_key or (digest(snapshot) if reuse else identity())
    previous = catalog.rows("SELECT id FROM audiobook_runs WHERE request_key=?", (key,))
    if previous:
        return catalog.run(previous[0]["id"])
    run_id = identity()
    run_key = f"data/runs/{run_id}"
    run_root = inside(root, run_key)
    run_root.parent.mkdir(parents=True, exist_ok=True)
    staged = Path(tempfile.mkdtemp(prefix=".prepare-", dir=run_root.parent))
    try:
        (staged / "config").mkdir()
        for name, content in configuration.items():
            (staged / "config" / name).write_text(content)
        book_dir = staged / "data/book" / slug
        book_dir.mkdir(parents=True)
        publish_text(book_dir / "chapters.json", encode(payload))
        for name, content in overrides.items():
            publish_text(book_dir / name, content)
        if book["plan_id"] and not model:
            plan = catalog.one("SELECT * FROM chunk_plans WHERE id=?", (book["plan_id"],))
            plan_meta = json.loads(plan["meta_json"])
            plan_meta.update({"model": selected, "cast": cast, "voice": chosen_voice, "source_file": meta.get("source_file") or ""})
            publish_text(book_dir / "book.json", encode(plan_meta))
            chunks = catalog.rows("SELECT snapshot_json FROM chunks WHERE plan_id=? ORDER BY position", (book["plan_id"],))
            publish_text(book_dir / "chunks.jsonl", "".join(c["snapshot_json"] + "\n" for c in chunks))
        revision = catalog.one("SELECT * FROM book_revisions WHERE id=?", (text["revision_id"],))
        if revision["source_asset_id"] and meta.get("source_file"):
            destination = inside(staged, meta["source_file"])
            destination.parent.mkdir(parents=True, exist_ok=True)
            place(catalog.asset_path(revision["source_asset_id"]), destination)
        for name, revision_id in revisions.items():
            revision = catalog.one("SELECT profile_json FROM voice_revisions WHERE id=?", (revision_id,))
            profile_path = staged / "data/voices" / f"{name}.json"
            profile_path.parent.mkdir(parents=True, exist_ok=True)
            publish_text(profile_path, revision["profile_json"])
            for ref in catalog.rows("SELECT * FROM voice_references WHERE revision_id=?", (revision_id,)):
                if ref["asset_id"]:
                    target = inside(staged, ref["path"])
                    target.parent.mkdir(parents=True, exist_ok=True)
                    place(catalog.asset_path(ref["asset_id"]), target)
        staged.rename(run_root)
        model_id = catalog.model(selected)
        stamp = now()
        with catalog.db.write() as conn:
            # Submission identity is checked under the same write lock as insertion.
            existing = conn.execute("SELECT id FROM audiobook_runs WHERE request_key=?", (key,)).fetchone()
            if existing:
                shutil.rmtree(run_root)
                run_id = existing[0]
            else:
                conn.execute("INSERT INTO audiobook_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                             (run_id, book["book_id"], text["id"], None, model_id, key, run_key,
                              "pending", encode(snapshot), stamp, stamp, ""))
                for role, name in cast.items():
                    conn.execute("INSERT INTO run_voices VALUES (?,?,?,?)", (run_id, role, revisions[name],
                                 encode(cast_settings.get(role, {}))))
        # Register the copied plan under this run's precise model/cast snapshot.
        if (run_root / "data/book" / slug / "book.json").exists() and run_id == run_root.name:
            attach_plan(catalog, run_id)
        return catalog.run(run_id)
    except BaseException as exc:
        shutil.rmtree(staged, ignore_errors=True)
        with catalog.db.write() as conn:
            saved = conn.execute("SELECT id FROM audiobook_runs WHERE id=?", (run_root.name,)).fetchone()
            if saved:
                conn.execute("UPDATE audiobook_runs SET status='failed',error=?,updated_at=? WHERE id=?",
                             (str(exc), now(), run_root.name))
            else:
                shutil.rmtree(run_root, ignore_errors=True)
        raise


def attach_plan(catalog: Catalog, run_id: str) -> str:
    run = catalog.run(run_id)
    book_dir = inside(catalog.root, run["root_key"]) / "data/book" / run["slug"]
    meta = read_json(book_dir / "book.json")
    if meta is None:
        raise StorageError("chunking did not produce a book manifest")
    snapshot = json.loads(run["snapshot_json"])
    # Keep the approved assignments even when the chunker inferred new roles.
    cast = snapshot["cast"]
    meta["cast"] = {role: cast.get(role) or cast.get("narrator") for role in meta.get("cast", cast)}
    meta["voice"] = snapshot["voice"]
    meta["cast_settings"] = snapshot["cast_settings"]
    meta["model"] = snapshot["model"]
    publish_text(book_dir / "book.json", encode(meta))
    plan_id = catalog.register_plan(run["text_version_id"], meta, read_lines(book_dir / "chunks.jsonl"))
    with catalog.db.write() as conn:
        prior = conn.execute("SELECT plan_id FROM audiobook_runs WHERE id=?", (run_id,)).fetchone()[0]
        if prior and prior != plan_id:
            raise StorageError("an existing run cannot change its chunk plan; prepare a new run")
        conn.execute("UPDATE audiobook_runs SET plan_id=?,updated_at=? WHERE id=?", (plan_id, now(), run_id))
    return plan_id


def execute_stage(root: Path, run_id: str, stage: str, *, fmt: str = "", device: str = "auto",
                  only: str = "", sample: str = "0", limit: int = 0, strict: bool = False) -> int:
    import fcntl
    catalog = Catalog(root)
    run_root = inside(root, catalog.run(run_id)["root_key"])
    with (run_root / ".execution.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise StorageError("this audiobook run already has an active stage") from exc
        try:
            return _execute_stage(root, run_id, stage, fmt=fmt, device=device, only=only, sample=sample, limit=limit, strict=strict)
        except (ValueError, OSError) as exc:
            with catalog.db.write() as conn:
                conn.execute("UPDATE audiobook_runs SET status='failed',error=?,updated_at=? WHERE id=?", (str(exc), now(), run_id))
                conn.execute("UPDATE job_attempts SET status='failed',error=?,finished_at=? WHERE run_id=? AND status IN ('running','launching')",
                             (str(exc), now(), run_id))
            print(str(exc), file=sys.stderr)
            return 1


def _execute_stage(root: Path, run_id: str, stage: str, *, fmt: str = "", device: str = "auto",
                  only: str = "", sample: str = "0", limit: int = 0, strict: bool = False) -> int:
    if stage not in STAGES:
        raise StorageError(f"unsupported run stage: {stage}")
    catalog = Catalog(root)
    run = catalog.run(run_id)
    if run["status"] == "legacy":
        raise StorageError("historical audio is archived; prepare a new run to narrate this book")
    run_root = inside(root, run["root_key"])
    snapshot = json.loads(run["snapshot_json"])
    slug = run["slug"]
    attempt_id = os.environ.get("AF_ATTEMPT_ID") or identity()
    with catalog.db.write() as conn:
        conn.execute("INSERT OR IGNORE INTO job_attempts(id,run_id,stage,status,started_at) VALUES (?,?,?,?,?)",
                     (attempt_id, run_id, stage, "running", now()))
        conn.execute("UPDATE job_attempts SET status='running',pid=? WHERE id=?", (os.getpid(), attempt_id))
        if stage == "chunk" and run["plan_id"]:
            conn.execute("UPDATE job_attempts SET status='done',finished_at=? WHERE id=?", (now(), attempt_id))
            return 0
        conn.execute("UPDATE audiobook_runs SET status='running',error='',updated_at=? WHERE id=?", (now(), run_id))
    env = dict(os.environ, AUDIOBOOK_FACTORY_ROOT=str(run_root), AF_CATALOG_STAGE="1")
    # Commands live in the checkout, while every data/config path resolves in the run.
    checkout = Path(__file__).resolve().parents[4]
    if stage == "chunk":
        command = [sys.executable, "-m", "bookbinder.chunk", slug, "--voice", snapshot["voice"],
                   "--model", snapshot["model"]["id"], "--max-chars", str(snapshot["model"]["char_limit"])]
        if snapshot.get("single_voice"):
            command += ["--single-voice"]
    else:
        environment = {"synth": snapshot["model"]["environment"], "verify": "transcriber"}.get(stage, "bookbinder")
        interpreter = checkout / "apps" / environment / ".venv/bin/python"
        if not interpreter.is_file():
            raise StorageError(f"missing {environment} environment; run the project setup")
        module = {"synth": "narrator.synth", "verify": "transcriber.verify",
                  "dryrun": "bookbinder.dryrun", "assemble": "bookbinder.assemble"}[stage]
        command = [str(interpreter), "-m", module, slug]
        if stage == "synth":
            command += ["--voice", snapshot["voice"], "--device", device]
            env["COQUI_TOS_AGREED"] = "1"
            if only:
                command += ["--only", only]
            if limit:
                command += ["--limit", str(limit)]
        if stage == "dryrun" and strict:
            command += ["--strict"]
        if stage == "assemble":
            command += ["--fmt", fmt]
        if stage == "verify" and sample != "0":
            command += ["--sample", sample]
    code, error = 1, ""
    try:
        if stage != "chunk" and not run["plan_id"]:
            raise StorageError("chunk this run before producing audio")
        if stage in ("synth", "assemble"):
            from bookbinder.preflight import assembly, synthesis, require
            require(assembly(run_root, slug, fmt) if stage == "assemble" else synthesis(run_root, slug))
        process = subprocess.Popen(command, cwd=checkout, env=env)
        try:
            while True:
                try:
                    code = process.wait(timeout=5)
                    break
                except subprocess.TimeoutExpired:
                    if stage == "synth":
                        catalog.register_results(run_id, refresh=False)
        except BaseException:
            process.terminate()
            process.wait()
            raise
        if code == 0:
            if stage == "chunk":
                attach_plan(catalog, run_id)
            else:
                catalog.register_results(run_id)
        else:
            error = f"{stage} exited with status {code}"
    except (ValueError, OSError) as exc:
        code, error = 1, str(exc)
    finally:
        with catalog.db.write() as conn:
            conn.execute("UPDATE job_attempts SET status=?,error=?,finished_at=? WHERE id=?",
                         ("done" if code == 0 else "failed", error, now(), attempt_id))
            fragments = conn.execute("SELECT chunk_id,asset_id,fingerprint FROM render_fragments WHERE run_id=?", (run_id,)).fetchall()
            current_audio = digest(sorted(tuple(row) for row in fragments))
            exported = any(json.loads(row[0]).get("fragments") == current_audio
                           for row in conn.execute("SELECT metadata_json FROM exports WHERE run_id=?", (run_id,)))
            conn.execute("UPDATE audiobook_runs SET status=?,error=?,updated_at=? WHERE id=?",
                         ("done" if code == 0 and exported and stage in ("assemble", "verify") else "pending" if code == 0 else "failed",
                          error, now(), run_id))
    if error:
        print(error, file=sys.stderr)
    return code
