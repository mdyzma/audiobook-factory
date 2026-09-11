CREATE TABLE assets (
    id TEXT PRIMARY KEY, sha256 TEXT NOT NULL UNIQUE,
    storage_key TEXT NOT NULL UNIQUE, size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
    suffix TEXT NOT NULL, created_at TEXT NOT NULL, media_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE books (
    id TEXT PRIMARY KEY, slug TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
    author TEXT NOT NULL DEFAULT '', current_text_id TEXT REFERENCES text_versions(id),
    current_plan_id TEXT REFERENCES chunk_plans(id), created_at TEXT NOT NULL
);
CREATE TABLE book_revisions (
    id TEXT PRIMARY KEY, book_id TEXT NOT NULL REFERENCES books(id),
    source_asset_id TEXT REFERENCES assets(id), fingerprint TEXT NOT NULL,
    original_source TEXT NOT NULL, recorded_sha256 TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL, UNIQUE(book_id, fingerprint)
);
CREATE TABLE text_versions (
    id TEXT PRIMARY KEY, revision_id TEXT NOT NULL REFERENCES book_revisions(id),
    fingerprint TEXT NOT NULL, language TEXT NOT NULL DEFAULT '',
    encoding_json TEXT NOT NULL, language_json TEXT NOT NULL,
    review_json TEXT NOT NULL, snapshot_json TEXT NOT NULL,
    created_at TEXT NOT NULL, UNIQUE(revision_id, fingerprint)
);
CREATE TABLE chapters (
    id TEXT PRIMARY KEY, text_version_id TEXT NOT NULL REFERENCES text_versions(id),
    position INTEGER NOT NULL CHECK(position >= 0), title TEXT NOT NULL,
    paragraphs_json TEXT NOT NULL, source_ref TEXT NOT NULL,
    UNIQUE(text_version_id, position)
);
CREATE TABLE model_snapshots (
    id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL UNIQUE, snapshot_json TEXT NOT NULL
);
CREATE TABLE chunk_plans (
    id TEXT PRIMARY KEY, text_version_id TEXT NOT NULL REFERENCES text_versions(id),
    model_snapshot_id TEXT NOT NULL REFERENCES model_snapshots(id),
    fingerprint TEXT NOT NULL, meta_json TEXT NOT NULL,
    created_at TEXT NOT NULL, UNIQUE(text_version_id, fingerprint)
);
CREATE TABLE chunks (
    id TEXT PRIMARY KEY, plan_id TEXT NOT NULL REFERENCES chunk_plans(id),
    chapter_id TEXT NOT NULL REFERENCES chapters(id),
    chunk_key TEXT NOT NULL, position INTEGER NOT NULL CHECK(position >= 0),
    chapter_index INTEGER NOT NULL, source_text TEXT NOT NULL, spoken_text TEXT NOT NULL,
    language TEXT NOT NULL, role TEXT NOT NULL, snapshot_json TEXT NOT NULL,
    UNIQUE(plan_id, chunk_key), UNIQUE(plan_id, position)
);
CREATE TABLE voices (id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE);
CREATE TABLE voice_revisions (
    id TEXT PRIMARY KEY, voice_id TEXT NOT NULL REFERENCES voices(id),
    fingerprint TEXT NOT NULL, profile_json TEXT NOT NULL, created_at TEXT NOT NULL,
    UNIQUE(voice_id, fingerprint)
);
CREATE TABLE voice_references (
    revision_id TEXT NOT NULL REFERENCES voice_revisions(id),
    path TEXT NOT NULL, asset_id TEXT REFERENCES assets(id),
    kind TEXT NOT NULL, PRIMARY KEY(revision_id, path)
);
CREATE TABLE audiobook_runs (
    id TEXT PRIMARY KEY, book_id TEXT NOT NULL REFERENCES books(id),
    text_version_id TEXT NOT NULL REFERENCES text_versions(id),
    plan_id TEXT REFERENCES chunk_plans(id), model_snapshot_id TEXT REFERENCES model_snapshots(id),
    request_key TEXT NOT NULL UNIQUE, root_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK(status IN ('pending','running','done','failed','cancelled','legacy')),
    snapshot_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX runs_book ON audiobook_runs(book_id, created_at);
CREATE TABLE run_voices (
    run_id TEXT NOT NULL REFERENCES audiobook_runs(id), role TEXT NOT NULL,
    voice_revision_id TEXT NOT NULL REFERENCES voice_revisions(id),
    settings_json TEXT NOT NULL DEFAULT '{}', PRIMARY KEY(run_id, role)
);
CREATE TABLE render_fragments (
    run_id TEXT NOT NULL REFERENCES audiobook_runs(id), chunk_id TEXT NOT NULL REFERENCES chunks(id),
    asset_id TEXT NOT NULL REFERENCES assets(id), fingerprint TEXT NOT NULL,
    duration_sec REAL NOT NULL CHECK(duration_sec >= 0), snapshot_json TEXT NOT NULL,
    PRIMARY KEY(run_id, chunk_id)
);
CREATE TRIGGER fragment_plan BEFORE INSERT ON render_fragments
WHEN (SELECT plan_id FROM audiobook_runs WHERE id=NEW.run_id) IS NOT
     (SELECT plan_id FROM chunks WHERE id=NEW.chunk_id)
BEGIN SELECT RAISE(ABORT, 'fragment belongs to another chunk plan'); END;
CREATE TABLE exports (
    id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES audiobook_runs(id),
    asset_id TEXT NOT NULL REFERENCES assets(id), format TEXT NOT NULL,
    metadata_json TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(run_id, asset_id)
);
CREATE TABLE qa_runs (
    id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES audiobook_runs(id),
    fingerprint TEXT NOT NULL, report_json TEXT NOT NULL, created_at TEXT NOT NULL,
    UNIQUE(run_id, fingerprint)
);
CREATE TABLE run_reports (
    run_id TEXT NOT NULL REFERENCES audiobook_runs(id), name TEXT NOT NULL,
    content_json TEXT NOT NULL, PRIMARY KEY(run_id, name)
);
CREATE TABLE import_batches (id TEXT PRIMARY KEY, folder TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE import_items (
    id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES import_batches(id),
    position INTEGER NOT NULL, source_path TEXT NOT NULL, asset_id TEXT REFERENCES assets(id),
    book_id TEXT REFERENCES books(id), text_version_id TEXT REFERENCES text_versions(id),
    status TEXT NOT NULL CHECK(status IN ('imported','review','failed','unchanged','duplicate','unsupported')),
    details_json TEXT NOT NULL, UNIQUE(batch_id, position)
);
CREATE TABLE items (
    id INTEGER PRIMARY KEY AUTOINCREMENT, slug TEXT NOT NULL, action TEXT NOT NULL,
    args TEXT NOT NULL DEFAULT '{}', position INTEGER NOT NULL, batch TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending', claim TEXT NOT NULL DEFAULT '',
    claimed_at TEXT NOT NULL DEFAULT '', job_id TEXT NOT NULL DEFAULT '', attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
    run_id TEXT REFERENCES audiobook_runs(id)
);
CREATE INDEX items_ready ON items(status, id);
CREATE INDEX items_book ON items(slug, position);
CREATE INDEX items_run ON items(run_id);
CREATE TABLE job_attempts (
    id TEXT PRIMARY KEY, run_id TEXT REFERENCES audiobook_runs(id),
    queue_id INTEGER REFERENCES items(id), stage TEXT NOT NULL, job_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL, error TEXT NOT NULL DEFAULT '', started_at TEXT NOT NULL, finished_at TEXT,
    pid INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO meta VALUES ('schema_version', '1');
CREATE TRIGGER chunk_chapter BEFORE INSERT ON chunks
WHEN (SELECT text_version_id FROM chapters WHERE id=NEW.chapter_id) IS NOT
     (SELECT text_version_id FROM chunk_plans WHERE id=NEW.plan_id)
BEGIN SELECT RAISE(ABORT, 'chapter belongs to another text version'); END;
CREATE TRIGGER run_text BEFORE INSERT ON audiobook_runs
WHEN NEW.book_id IS NOT (SELECT r.book_id FROM book_revisions r JOIN text_versions t ON t.revision_id=r.id WHERE t.id=NEW.text_version_id)
BEGIN SELECT RAISE(ABORT, 'text belongs to another book'); END;
CREATE TRIGGER run_plan BEFORE UPDATE OF plan_id ON audiobook_runs
WHEN (OLD.plan_id IS NOT NULL AND NEW.plan_id IS NOT OLD.plan_id) OR
     (NEW.plan_id IS NOT NULL AND NEW.text_version_id IS NOT (SELECT text_version_id FROM chunk_plans WHERE id=NEW.plan_id))
BEGIN SELECT RAISE(ABORT, 'run plan is immutable and must belong to its text'); END;
CREATE TRIGGER run_snapshot BEFORE UPDATE OF book_id,text_version_id,model_snapshot_id,request_key,root_key,snapshot_json ON audiobook_runs
BEGIN SELECT RAISE(ABORT, 'approved run inputs are immutable'); END;
