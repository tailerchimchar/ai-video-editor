"""Shared test fixtures.

Each integration test gets a fresh temp SQLite DB and workspace dir.
Heavy outside-world calls (ffmpeg, Whisper, Anthropic, sqlite-vec
embeddings) are monkeypatched in the test that needs them — these
fixtures only set up the sandbox and pre-populate an asset.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Temp DB + workspace, isolated per test."""
    from ai_video_editor.config import settings

    db = tmp_path / "test.db"
    ws = tmp_path / "ws"
    monkeypatch.setattr(settings, "database_path", str(db))
    monkeypatch.setattr(settings, "workspace_dir", ws)
    # Disable Langfuse during tests — no network, no surprises.
    monkeypatch.setattr(settings, "langfuse_public_key", "")
    monkeypatch.setattr(settings, "langfuse_secret_key", "")
    return tmp_path


@pytest.fixture
def client(sandbox):
    """TestClient with the lifespan run against the temp sandbox."""
    from ai_video_editor.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def asset(sandbox):
    """A fake asset row in the temp DB. The path doesn't have to exist
    on disk for the endpoints under test; we mock anything that probes
    it. Returns the asset dict."""
    import asyncio

    from ai_video_editor.database import get_db

    a = {
        "id": str(uuid.uuid4()),
        "filename": "League of Legends_05-15-2026_21-19-8-0.mp4",
        "path": str(sandbox / "fake.mp4"),
        "game": "League of Legends",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "indexed_at": datetime.now(timezone.utc).isoformat(),
    }

    async def _insert():
        db = await get_db()
        try:
            await db.execute(
                "INSERT INTO assets (id, filename, path, game, created_at, indexed_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (a["id"], a["filename"], a["path"], a["game"], a["created_at"], a["indexed_at"]),
            )
            await db.commit()
        finally:
            await db.close()

    asyncio.run(_insert())
    return a


@pytest.fixture
def poll():
    """Returns a helper that polls a job until terminal status."""
    import time

    def _poll(client: TestClient, job_id: str, attempts: int = 60) -> dict:
        last = None
        for _ in range(attempts):
            last = client.get(f"/api/v1/jobs/{job_id}").json()
            if last["status"] in ("completed", "failed"):
                return last
            time.sleep(0.05)
        raise AssertionError(f"job {job_id} did not finish: {last}")

    return _poll
