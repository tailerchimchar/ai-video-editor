import asyncio
import sqlite3

from ai_video_editor import database
from ai_video_editor.config import settings

OLD_DDL = """
CREATE TABLE transcripts (
    id TEXT PRIMARY KEY, video_id TEXT NOT NULL, start_seconds REAL NOT NULL,
    end_seconds REAL NOT NULL, text TEXT NOT NULL, sentiment_score REAL,
    words TEXT, created_at TEXT NOT NULL
);
"""


def _cols(path):
    c = sqlite3.connect(path)
    try:
        return {r[1] for r in c.execute("PRAGMA table_info(transcripts)")}
    finally:
        c.close()


def test_speaker_column_added_idempotently(tmp_path, monkeypatch):
    db = tmp_path / "old.db"
    c = sqlite3.connect(db)
    c.executescript(OLD_DDL)
    c.execute("INSERT INTO transcripts VALUES ('1','v',0,1,'old',NULL,NULL,'x')")
    c.commit()
    c.close()
    monkeypatch.setattr(settings, "database_path", db)
    asyncio.run(database.init_db())
    asyncio.run(database.init_db())  # second run must not raise
    assert "speaker" in _cols(db)
    c = sqlite3.connect(db)
    assert c.execute("SELECT speaker FROM transcripts WHERE id='1'").fetchone() == (None,)
    c.execute("INSERT INTO transcripts VALUES ('2','v',0,1,'me',NULL,NULL,'x','owner')")
    c.commit()
    c.close()
