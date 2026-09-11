"""Versioned library records; immutable files carry the large binary payloads."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any

from studio.database import Database, StorageError, now


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(encode(value).encode()).hexdigest()


def identity() -> str:
    return uuid.uuid4().hex


def read_json(path: Path, default: Any = None) -> Any:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else default


def read_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.is_file() else []


def relink(source: Path, stored: Path) -> bool:
    """Point `source` at the same bytes as `stored`, instead of a second copy.

    A rendered fragment lands in the run's own data root and is then published
    to the asset store, which left two identical files on disk for every
    fragment ever produced. A twenty-hour book is several gigabytes of them,
    and comparing two voices doubles that again, so the duplicate is not a
    rounding error.

    A hard link is the right tool because both live under `data/` on one
    filesystem and the content is immutable by construction: the asset's name
    is the hash of its bytes. Every stage that writes one of these names
    publishes by renaming into place, which replaces the directory entry and
    leaves the shared inode alone, so nothing can rewrite the stored copy
    through the run's name.

    Best effort by design. If the two are already the same file, or the link
    cannot be made, the copy stays and nothing is lost but the space.
    """
    try:
        if not source.is_file() or not stored.is_file():
            return False
        if source.samefile(stored):
            return True
        staged = source.with_name(f".{source.name}.link")
        staged.unlink(missing_ok=True)
        os.link(stored, staged)
        staged.replace(source)
        return True
    except OSError:
        return False


def place(stored: Path, destination: Path) -> None:
    """Put a stored asset where a stage expects to find it, without copying.

    Preparing a run materialises the source text and every voice reference into
    the run's own data root. Copying them means a second set of bytes per run,
    which for a cast of several voices is most of the conditioning audio again
    each time. The stored asset is immutable and the stages read these, so one
    inode under two names is the whole requirement.
    """
    destination.unlink(missing_ok=True)
    try:
        os.link(stored, destination)
    except OSError:
        shutil.copy2(stored, destination)


def inside(root: Path, key: str) -> Path:
    path = (root / key).resolve()
    if not path.is_relative_to(root.resolve()):
        raise StorageError(f"file is outside the library: {key}")
    return path


def legacy_source(root: Path, key: str) -> Path | None:
    """Old manifests used absolute checkout paths and ../data paths.

    Rebase only into this library's data directory; never read an unrelated
    external path while restoring or importing an old manifest.
    """
    if not key:
        return None
    try:
        candidate = inside(root, key)
        if candidate.is_file():
            return candidate
    except StorageError:
        pass
    parts = Path(key).parts
    if "data" in parts:
        try:
            candidate = inside(root, str(Path(*parts[parts.index("data"):])) )
            if candidate.is_file():
                return candidate
        except StorageError:
            pass
    return None


class Catalog:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.db = Database(self.root)
        self._audio_seen: dict[tuple[str, str], tuple[int, int, str]] = {}

    def rows(self, sql: str, args: tuple = ()) -> list[dict]:
        with self.db.connect() as conn:
            return [dict(row) for row in conn.execute(sql, args)]

    def one(self, sql: str, args: tuple = ()) -> dict:
        rows = self.rows(sql, args)
        if not rows:
            raise StorageError("the requested library record does not exist")
        return rows[0]

    def asset(self, source: Path, collapse: bool = False) -> str:
        """Hash the exact bytes copied, then publish once before registering.

        `collapse` folds the source into the stored copy afterwards, leaving
        one set of bytes under two names instead of two. Ask for it only where
        the source is a run output this pipeline publishes by rename: a
        rendered fragment or a finished export. A voice reference a person may
        edit, or a book still sitting in someone's Downloads folder, must keep
        its own bytes, or editing it would rewrite an asset whose name is the
        hash of what it used to contain.
        """
        store = self.root / "data/assets"
        store.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=store, prefix=".incoming-")
        staged = Path(name)
        try:
            hashed = hashlib.sha256()
            size = 0
            with os.fdopen(fd, "wb") as out, source.open("rb") as inp:
                for block in iter(lambda: inp.read(1 << 20), b""):
                    hashed.update(block)
                    size += len(block)
                    out.write(block)
                out.flush()
                os.fsync(out.fileno())
            key = hashed.hexdigest()
            suffix = source.suffix.lower()
            # The content hash, not the filename, is the identity.
            existing = self.rows("SELECT * FROM assets WHERE id=?", (key,))
            if existing:
                # Also heals a missing or externally damaged stored copy.
                destination = inside(self.root, existing[0]["storage_key"])
                staged.replace(destination)
                if collapse:
                    self._collapse(source, destination)
                return key
            destination = store / key[:2] / (key + suffix)
            destination.parent.mkdir(parents=True, exist_ok=True)
            # replace is safe here: every publisher of this name has identical bytes.
            staged.replace(destination)
            if collapse:
                self._collapse(source, destination)
            media: dict = {}
            if suffix == ".wav":
                import wave
                try:
                    with wave.open(str(destination), "rb") as wav:
                        media = {"sample_rate": wav.getframerate(), "channels": wav.getnchannels(),
                                 "duration_sec": wav.getnframes() / wav.getframerate(), "sample_width": wav.getsampwidth()}
                except (wave.Error, EOFError):
                    pass
            elif suffix in (".mp3", ".m4b"):
                import subprocess
                try:
                    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                                            "format=duration:stream=codec_name,sample_rate,channels", "-of", "json", str(destination)],
                                           capture_output=True, text=True, timeout=15, check=False)
                    if probe.returncode == 0:
                        media = json.loads(probe.stdout)
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    pass
            with self.db.write() as conn:
                conn.execute("INSERT OR IGNORE INTO assets VALUES (?,?,?,?,?,?,?)",
                             (key, key, destination.relative_to(self.root).as_posix(), size, suffix, now(), encode(media)))
            return key
        finally:
            staged.unlink(missing_ok=True)

    def _collapse(self, source: Path, stored: Path) -> None:
        """Fold a just-published file into the one copy the store now holds.

        Only for sources already inside the library: an imported book still
        sitting in someone's Downloads folder is theirs, and the catalog has no
        business rewriting it into a link to its own store.
        """
        try:
            source.resolve().relative_to(self.root)
        except ValueError:
            return
        relink(source, stored)

    def asset_path(self, asset_id: str) -> Path:
        row = self.one("SELECT storage_key FROM assets WHERE id=?", (asset_id,))
        return inside(self.root, row["storage_key"])

    def model(self, snapshot: dict) -> str:
        key = digest(snapshot)
        with self.db.write() as conn:
            conn.execute("INSERT OR IGNORE INTO model_snapshots VALUES (?,?,?)", (key, key, encode(snapshot)))
        return key

    def register_book(self, slug: str, source_root: Path | None = None,
                      select_current: bool = True) -> dict:
        from studio.data import check_name
        check_name(slug)
        base = source_root or self.root
        folder = inside(base, f"data/book/{slug}")
        payload = read_json(folder / "chapters.json", {})
        meta = read_json(folder / "book.json", {})
        chunks = read_lines(folder / "chunks.jsonl")
        extracted = payload.get("meta") or meta
        if not extracted:
            raise StorageError(f"{slug}: no readable book metadata")
        source_key = extracted.get("source_file") or ""
        source = legacy_source(base, source_key)
        source_id = self.asset(source) if source and source.is_file() else None
        recorded_hash = extracted.get("source_sha256") or ""
        if recorded_hash and source_id and recorded_hash.lower() != source_id:
            source_id = None
        revision_fp = source_id or recorded_hash or digest({"legacy_slug": slug, "source": source_key})
        chapters = payload.get("chapters") or []
        if not payload:
            for index in sorted({int(c.get("chapter_index", 0)) for c in chunks}):
                members = [c for c in chunks if c.get("chapter_index", 0) == index]
                chapters.append({"index": index, "title": members[0].get("chapter_title", ""),
                                 "paragraphs": [c.get("source_text") or c["text"] for c in members],
                                 "source_ref": ""})
        text_snapshot = {"meta": extracted, "chapters": chapters}
        # Storage relocation and casting do not change the prepared book text.
        text_meta = {k: v for k, v in extracted.items() if k not in
                     ("source_file", "original_source", "model", "voice", "cast", "cast_settings", "chunk_count", "est_hours")}
        text_fp = digest({"meta": text_meta, "chapters": chapters})
        stamp = now()
        with self.db.write() as conn:
            conn.execute("INSERT OR IGNORE INTO books(id,slug,title,author,created_at) VALUES (?,?,?,?,?)",
                         (identity(), slug, extracted.get("title") or slug, extracted.get("author") or "", stamp))
            book_id = conn.execute("SELECT id FROM books WHERE slug=?", (slug,)).fetchone()[0]
            conn.execute("INSERT OR IGNORE INTO book_revisions VALUES (?,?,?,?,?,?,?)",
                         (identity(), book_id, source_id, revision_fp,
                          extracted.get("original_source") or source_key, recorded_hash, stamp))
            revision_id = conn.execute("SELECT id FROM book_revisions WHERE book_id=? AND fingerprint=?",
                                       (book_id, revision_fp)).fetchone()[0]
            review = list(extracted.get("review_reasons") or [])
            if not source_id:
                review.append("original source unavailable; preserved legacy text")
            conn.execute("INSERT OR IGNORE INTO text_versions VALUES (?,?,?,?,?,?,?,?,?)",
                         (identity(), revision_id, text_fp, extracted.get("language") or "",
                          encode(extracted.get("encoding") or {}), encode(extracted.get("language_decision") or {}),
                          encode(review), encode(text_snapshot), stamp))
            text_id = conn.execute("SELECT id FROM text_versions WHERE revision_id=? AND fingerprint=?",
                                   (revision_id, text_fp)).fetchone()[0]
            for ordinal, chapter in enumerate(chapters):
                conn.execute("INSERT OR IGNORE INTO chapters VALUES (?,?,?,?,?,?)",
                             (identity(), text_id, ordinal, chapter.get("title") or "",
                              encode(chapter.get("paragraphs") or []), chapter.get("source_ref") or ""))
            if select_current:
                conn.execute("UPDATE books SET title=?,author=?,current_text_id=?,"
                             "current_plan_id=CASE WHEN current_text_id=? THEN current_plan_id ELSE NULL END WHERE id=?",
                             (extracted.get("title") or slug, extracted.get("author") or "", text_id, text_id, book_id))
        plan_id = None
        # Old chunk metadata must not become the plan of newly imported text.
        same_source = not payload or all(not meta.get(k) or not extracted.get(k) or meta.get(k) == extracted.get(k)
                                        for k in ("source_sha256", "language", "encoding", "language_decision"))
        if chunks and meta and same_source and not (folder / ".needs-chunking").exists():
            plan_id = self.register_plan(text_id, meta, chunks)
            if select_current:
                with self.db.write() as conn:
                    conn.execute("UPDATE books SET current_plan_id=? WHERE id=?", (plan_id, book_id))
        return {"book_id": book_id, "revision_id": revision_id, "text_version_id": text_id, "plan_id": plan_id}

    def register_plan(self, text_id: str, meta: dict, chunks: list[dict]) -> str:
        from bookbinder.manifest import Chunk
        validated = [Chunk.model_validate(chunk).model_dump() for chunk in chunks]
        limit = (meta.get("model") or {}).get("char_limit") or 0
        if limit and any(len(chunk["text"]) > limit for chunk in validated):
            raise StorageError("chunk exceeds the selected model's input limit; create a new chunk plan")
        if len({c["id"] for c in validated}) != len(validated) or len({c["order"] for c in validated}) != len(validated):
            raise StorageError("chunk plan has duplicate identities or positions")
        model_id = self.model(meta.get("model") or {})
        fp = digest({"meta": meta, "chunks": validated})
        source = json.loads(self.one("SELECT snapshot_json FROM text_versions WHERE id=?", (text_id,))["snapshot_json"])
        chapter_indices = {position: chapter.get("index", position) for position, chapter in enumerate(source["chapters"])}
        chapters = {chapter_indices[row["position"]]: row["id"]
                    for row in self.rows("SELECT * FROM chapters WHERE text_version_id=?", (text_id,))}
        if any(chunk["chapter_index"] not in chapters for chunk in validated):
            raise StorageError("chunk refers to a chapter outside its text version")
        with self.db.write() as conn:
            conn.execute("INSERT OR IGNORE INTO chunk_plans VALUES (?,?,?,?,?,?)",
                         (identity(), text_id, model_id, fp, encode(meta), now()))
            plan_id = conn.execute("SELECT id FROM chunk_plans WHERE text_version_id=? AND fingerprint=?",
                                   (text_id, fp)).fetchone()[0]
            for chunk in validated:
                conn.execute("INSERT OR IGNORE INTO chunks VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                             (identity(), plan_id, chapters[chunk["chapter_index"]], chunk["id"], chunk["order"], chunk["chapter_index"],
                              chunk.get("source_text") or chunk["text"], chunk["text"], chunk["language"],
                              chunk["role"], encode(chunk)))
        return plan_id

    def register_voice(self, name: str) -> str:
        from studio.data import check_name
        check_name(name)
        profile = read_json(inside(self.root, f"data/voices/{name}.json"))
        if profile is None:
            raise StorageError(f"no saved voice named {name}")
        references: list[tuple[str, str | None, str]] = []
        for key in profile.get("reference_wavs") or []:
            path = inside(self.root, key)
            references.append((key, self.asset(path) if path.is_file() else None, "reference"))
        for filename in ("latents.pt", "audition.wav", "clone.json"):
            key = f"data/voices/{name}/{filename}"
            path = inside(self.root, key)
            if path.is_file():
                references.append((key, self.asset(path), "cache"))
        model_dir = profile.get("model_dir")
        if model_dir:
            directory = inside(self.root, model_dir)
            if not directory.is_dir():
                raise StorageError(f"voice model directory is missing: {model_dir}")
            for path in sorted(directory.rglob("*")):
                if path.is_file():
                    inside(self.root, str(path))
                    references.append((path.relative_to(self.root).as_posix(), self.asset(path), "model"))
        fp = digest({"profile": profile, "references": references})
        with self.db.write() as conn:
            conn.execute("INSERT OR IGNORE INTO voices VALUES (?,?)", (identity(), name))
            voice_id = conn.execute("SELECT id FROM voices WHERE name=?", (name,)).fetchone()[0]
            conn.execute("INSERT OR IGNORE INTO voice_revisions VALUES (?,?,?,?,?)",
                         (identity(), voice_id, fp, encode(profile), now()))
            revision_id = conn.execute("SELECT id FROM voice_revisions WHERE voice_id=? AND fingerprint=?", (voice_id, fp)).fetchone()[0]
            for path, asset_id, kind in references:
                conn.execute("INSERT OR IGNORE INTO voice_references VALUES (?,?,?,?)", (revision_id, path, asset_id, kind))
        return revision_id

    def books(self) -> list[dict]:
        return self.rows("SELECT b.*, t.language, t.encoding_json, t.review_json FROM books b "
                         "LEFT JOIN text_versions t ON t.id=b.current_text_id ORDER BY b.slug")

    def runs(self, slug: str = "") -> list[dict]:
        return self.rows("SELECT r.*,b.slug FROM audiobook_runs r JOIN books b ON b.id=r.book_id "
                         "WHERE (?='' OR b.slug=?) ORDER BY r.created_at DESC", (slug, slug))

    def run(self, run_id: str) -> dict:
        return self.one("SELECT r.*,b.slug FROM audiobook_runs r JOIN books b ON b.id=r.book_id WHERE r.id=?", (run_id,))

    def reconcile(self) -> dict:
        result: dict[str, Any] = {"books": 0, "voices": 0, "errors": []}
        for directory in sorted((self.root / "data/book").glob("*")):
            if directory.is_dir():
                try:
                    self.register_book(directory.name)
                    result["books"] += 1
                    from studio.legacy import archive_audio
                    archive_audio(self, directory.name)
                except (ValueError, OSError) as exc:
                    result["errors"].append(f"{directory.name}: {exc}")
        for path in sorted((self.root / "data/voices").glob("*.json")):
            try:
                self.register_voice(path.stem)
                result["voices"] += 1
            except (ValueError, OSError) as exc:
                result["errors"].append(f"{path.stem}: {exc}")
        return result

    def register_results(self, run_id: str, *, refresh: bool = True) -> dict:
        run = self.run(run_id)
        base = inside(self.root, run["root_key"])
        audio = base / "data/audio" / run["slug"]
        rendered = read_lines(audio / "rendered.jsonl")
        chunks = {row["chunk_key"]: row for row in self.rows("SELECT * FROM chunks WHERE plan_id=?", (run["plan_id"],))}
        # The renderer's ledger commits each WAV before the whole manifest lands.
        # Recover completed work even if the process dies before rendered.jsonl.
        if run["status"] != "legacy" and (audio / "fingerprints.jsonl").is_file():
            import wave
            ledger = {}
            for line in (audio / "fingerprints.jsonl").read_text().splitlines():
                try:
                    record = json.loads(line)
                    ledger[record["id"]] = record["fingerprint"]
                except (ValueError, KeyError):
                    continue
            current = {row["id"]: row for row in rendered}
            snapshot = json.loads(run["snapshot_json"])
            for key, fingerprint in ledger.items():
                if key not in chunks:
                    continue
                path = audio / f"{key}.wav"
                if not path.is_file():
                    continue
                row = json.loads(chunks[key]["snapshot_json"])
                row["voice"] = snapshot["voice"] or snapshot["cast"].get(row["role"]) or snapshot["cast"].get("narrator", "")
                with wave.open(str(path), "rb") as wav:
                    row["duration_sec"] = wav.getnframes() / wav.getframerate()
                row.update({"fingerprint": fingerprint, "audio_path": path.relative_to(base).as_posix()})
                current[key] = row
            rendered = list(current.values())
        if len({row.get("id") for row in rendered}) != len(rendered):
            raise StorageError("rendered manifest contains duplicate chunks")
        fragments = []
        voice_hashes: dict[str, str] = {}
        for row in rendered:
            chunk = chunks.get(row["id"])
            if chunk is None:
                raise StorageError(f"rendered chunk {row['id']} is outside this run's plan")
            path = inside(base, row.get("audio_path") or "")
            if not path.is_file():
                raise StorageError(f"missing rendered audio for {row['id']}")
            if run["status"] != "legacy" and not (audio / ".dry-run.json").is_file():
                from bookbinder.fingerprint import fragment_fingerprint, voice_revision
                from bookbinder.manifest import ModelChoice
                snapshot = json.loads(run["snapshot_json"])
                model = ModelChoice.model_validate(snapshot["model"])
                planned = json.loads(chunk["snapshot_json"])
                speaker = snapshot["voice"] or snapshot["cast"].get(planned["role"]) or snapshot["cast"].get("narrator", "")
                settings = dict(model.settings) | snapshot.get("cast_settings", {}).get(planned["role"], {})
                if speaker not in voice_hashes:
                    voice_hashes[speaker] = voice_revision(base, speaker)
                expected = fragment_fingerprint(text=planned["text"], language=planned["language"], model=model.identity,
                                                voice=speaker, voice_revision=voice_hashes[speaker], settings=settings)
                if row.get("voice") != speaker or row.get("fingerprint") != expected:
                    raise StorageError(f"rendered fragment {row['id']} does not match its approved inputs")
            stat = path.stat()
            seen = self._audio_seen.get((run_id, chunk["id"]))
            if not refresh and seen and seen[:2] == (stat.st_mtime_ns, stat.st_size):
                asset_id = seen[2]
            else:
                # Fragments are the bulk of a library: a twenty-hour book is
                # several gigabytes of them, and keeping a second copy per run
                # doubles that for every voice compared.
                asset_id = self.asset(path, collapse=True)
                stat = path.stat()       # the link carries the asset's mtime
            self._audio_seen[run_id, chunk["id"]] = (stat.st_mtime_ns, stat.st_size, asset_id)
            fragments.append((run_id, chunk["id"], asset_id, row.get("fingerprint") or "",
                              float(row.get("duration_sec") or 0), encode(row)))
        exports = []
        chapter_marks = read_json(base / "data/out" / f"{run['slug']}.chapters.json", [])
        assembly_metadata = {"chapters": chapter_marks,
                             "fragments": digest(sorted((f[1], f[2], f[3]) for f in fragments))}
        for path in (base / "data/out").glob(f"{run['slug']}.*"):
            if path.suffix in (".wav", ".mp3", ".m4b"):
                exports.append((identity(), run_id, self.asset(path, collapse=True),
                                path.suffix[1:], encode(assembly_metadata), now()))
        qa = read_json(audio / "qa_report.json")
        reports = {name: read_json(audio / name) for name in
                   ("report.json", "progress.json", ".dry-run.json", "qa_report.json") if (audio / name).is_file()}
        with self.db.write() as conn:
            for fragment in fragments:
                conn.execute("INSERT INTO render_fragments VALUES (?,?,?,?,?,?) ON CONFLICT(run_id,chunk_id) "
                             "DO UPDATE SET asset_id=excluded.asset_id,fingerprint=excluded.fingerprint,"
                             "duration_sec=excluded.duration_sec,snapshot_json=excluded.snapshot_json", fragment)
            for export in exports:
                conn.execute("INSERT OR IGNORE INTO exports VALUES (?,?,?,?,?,?)", export)
            if qa:
                conn.execute("INSERT OR IGNORE INTO qa_runs VALUES (?,?,?,?,?)", (identity(), run_id, digest(qa), encode(qa), now()))
            for name, content in reports.items():
                conn.execute("INSERT INTO run_reports VALUES (?,?,?) ON CONFLICT(run_id,name) "
                             "DO UPDATE SET content_json=excluded.content_json", (run_id, name, encode(content)))
        return {"fragments": len(fragments), "exports": len(exports), "qa": bool(qa)}
