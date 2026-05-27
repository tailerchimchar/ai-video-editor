import aiosqlite
import sqlite_vec

from .config import settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS assets (
    id TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    path TEXT NOT NULL UNIQUE,
    game TEXT,
    created_at TEXT NOT NULL,
    indexed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS clips (
    id TEXT PRIMARY KEY,
    asset_id TEXT NOT NULL REFERENCES assets(id),
    start_seconds REAL NOT NULL,
    end_seconds REAL NOT NULL,
    output_path TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS timeline_items (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    clip_id TEXT NOT NULL REFERENCES clips(id),
    position INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    project_id TEXT REFERENCES projects(id),
    type TEXT NOT NULL,
    status TEXT NOT NULL,
    output_path TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT
);

-- Phase 2: candidate-first highlight pipeline.
-- One row per potentially-interesting moment, from any source.
-- SQLite types chosen to port cleanly to Postgres later
-- (TEXT id -> uuid, TEXT metadata -> jsonb, TEXT created_at -> timestamptz).
CREATE TABLE IF NOT EXISTS highlight_candidates (
    id TEXT PRIMARY KEY,
    video_id TEXT NOT NULL REFERENCES assets(id),
    source TEXT NOT NULL,            -- see CandidateSource in models.py
    start_seconds REAL NOT NULL,
    end_seconds REAL NOT NULL,
    event_type TEXT,                 -- kill | death | funny_audio | laughter | unknown
    confidence REAL,
    metadata TEXT,                   -- JSON blob
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_candidates_video ON highlight_candidates(video_id);
CREATE INDEX IF NOT EXISTS idx_candidates_source ON highlight_candidates(source);

-- Whisper transcript segments (a reusable corpus: feeds the
-- transcript_keyword source now and RAG/semantic search later).
CREATE TABLE IF NOT EXISTS transcripts (
    id TEXT PRIMARY KEY,
    video_id TEXT NOT NULL REFERENCES assets(id),
    start_seconds REAL NOT NULL,
    end_seconds REAL NOT NULL,
    text TEXT NOT NULL,
    sentiment_score REAL,            -- VADER arousal [0,1]; NULL on legacy rows
    words TEXT,                      -- JSON [{word,start,end}]; NULL on pre-word-timestamps rows
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_transcripts_video ON transcripts(video_id);

-- Per-clip edits (zoom / caption / focus). Each row is one rendered
-- output file derived from a source asset + sub-range.
CREATE TABLE IF NOT EXISTS edits (
    id TEXT PRIMARY KEY,
    asset_id TEXT NOT NULL REFERENCES assets(id),
    kind TEXT NOT NULL,              -- zoom | caption | focus
    params TEXT NOT NULL,            -- JSON: start/end/factor/roi/etc.
    output_path TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_edits_asset ON edits(asset_id);

-- One compilation row per rendered reel (kept highlights → one .mp4).
CREATE TABLE IF NOT EXISTS compilations (
    id TEXT PRIMARY KEY,
    asset_id TEXT NOT NULL REFERENCES assets(id),
    output_path TEXT,                -- null while still rendering / on failure
    params TEXT NOT NULL,            -- JSON: aspect, order, music, etc.
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_compilations_asset ON compilations(asset_id);

-- RAG embeddings (sqlite-vec vec0 virtual table). The vector dim must
-- match settings.embed_dim (384 for the default bge-small model);
-- changing the model = re-index.
CREATE VIRTUAL TABLE IF NOT EXISTS embeddings USING vec0(
    chunk_id TEXT PRIMARY KEY,
    +video_id TEXT,
    +start_seconds FLOAT,
    +end_seconds FLOAT,
    +text TEXT,
    vector FLOAT[384]
);

-- User-edit feedback events. Every time the user manually adjusts the
-- system's output (extends a clip, removes one, reverts), we log it
-- here. Future jobs can aggregate per-event-type to auto-tune the
-- per-event windows (settings.event_window_overrides) and ranker bias.
--
-- One row = one user action. action ∈ {extend, remove_clip, revert,
-- reorder, edit_captions, add_caption, remove_caption, add_effect:*,
-- ...}. Mirrors the journal action keys so it's easy to correlate.
--
-- delta_before/delta_after are populated for `extend` actions in
-- seconds (positive = added time, negative = trimmed). For other
-- actions the columns are NULL.
CREATE TABLE IF NOT EXISTS feedback_events (
    id TEXT PRIMARY KEY,
    compilation_id TEXT NOT NULL,
    clip_id TEXT,                    -- nullable for compilation-level actions
    action TEXT NOT NULL,            -- mirrors journal action key
    event_type TEXT,                 -- clip's event_type at time of action (when known)
    delta_before REAL,               -- extend: seconds added BEFORE the clip's start
    delta_after REAL,                -- extend: seconds added AFTER the clip's end
    payload TEXT,                    -- JSON blob with full action details
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_feedback_compilation ON feedback_events(compilation_id);
CREATE INDEX IF NOT EXISTS idx_feedback_event_type ON feedback_events(event_type);
CREATE INDEX IF NOT EXISTS idx_feedback_action ON feedback_events(action);
"""


async def get_db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(settings.database_path)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    # Load the sqlite-vec extension so vec0 virtual tables + vector
    # functions are available on every connection.
    await db.enable_load_extension(True)
    await db.load_extension(sqlite_vec.loadable_path())
    await db.enable_load_extension(False)
    return db


async def init_db() -> None:
    db = await get_db()
    try:
        await db.executescript(_SCHEMA)
        # In-place migrations for existing DBs whose CREATE TABLE ran
        # before these columns were added. CREATE TABLE IF NOT EXISTS
        # is idempotent but doesn't add new columns to an old table.
        await _ensure_column(db, "transcripts", "sentiment_score", "REAL")
        await _ensure_column(db, "transcripts", "words", "TEXT")
        await db.commit()
    finally:
        await db.close()


async def _ensure_column(db, table: str, column: str, decl: str) -> None:
    """ALTER TABLE … ADD COLUMN, idempotent. SQLite's `PRAGMA
    table_info` lists current columns; ADD COLUMN with NULL default
    is non-destructive on existing rows."""
    rows = await db.execute_fetchall(f"PRAGMA table_info({table})")
    existing = {dict(r)["name"] for r in rows}
    if column not in existing:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
