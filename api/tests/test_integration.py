"""End-to-end tests of the job-shaped endpoints.

We mock the heavy/external steps (compute_candidates, rank_candidates,
embeddings, etc.) and verify the *wiring*: DB persistence, job status
transitions, response shapes. Real ffmpeg/Whisper/Anthropic are never
called.
"""

import uuid
from datetime import datetime, timezone


def test_candidates_endpoint_persists_rows(client, asset, poll, monkeypatch):
    # Mock compute_candidates inside the router (it's imported there).
    fake_rows = [
        {
            "id": str(uuid.uuid4()),
            "video_id": asset["id"],
            "source": "outplayed_clip",
            "start_seconds": 0.0,
            "end_seconds": 26.0,
            "event_type": "unknown",
            "confidence": 0.9,
            "metadata": {"rationale": "test"},
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    ]
    diag = {"riot": "disabled"}
    monkeypatch.setattr(
        "ai_video_editor.routers.analyze.compute_candidates",
        lambda asset, segments=None: (fake_rows, diag),
    )

    r = client.post(f"/api/v1/assets/{asset['id']}/candidates").json()
    job = poll(client, r["job_id"])
    assert job["status"] == "completed"
    assert "1 candidates" in job["output_path"]

    listed = client.get(f"/api/v1/assets/{asset['id']}/candidates").json()
    assert len(listed) == 1
    assert listed[0]["source"] == "outplayed_clip"


def test_rank_endpoint_writes_rankings_json(client, asset, poll, monkeypatch):
    # Pre-populate one candidate row so rank has something to rank.
    import asyncio

    from ai_video_editor.database import get_db
    from ai_video_editor.models import RankedCandidate

    cand_id = str(uuid.uuid4())

    async def _insert():
        db = await get_db()
        try:
            await db.execute(
                "INSERT INTO highlight_candidates "
                "(id, video_id, source, start_seconds, end_seconds, event_type, "
                " confidence, metadata, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    cand_id,
                    asset["id"],
                    "outplayed_clip",
                    0.0,
                    26.0,
                    "unknown",
                    0.9,
                    "{}",
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            await db.commit()
        finally:
            await db.close()

    asyncio.run(_insert())

    monkeypatch.setattr(
        "ai_video_editor.routers.analyze.rank_candidates",
        lambda game, candidates: [
            RankedCandidate(
                candidate_id=cand_id,
                keep=True,
                funny_score=0.4,
                hype_score=0.7,
                story_score=0.5,
                suggested_start_seconds=0.0,
                suggested_end_seconds=26.0,
                reason="test rank",
            )
        ],
    )

    r = client.post(f"/api/v1/assets/{asset['id']}/rank").json()
    job = poll(client, r["job_id"])
    assert job["status"] == "completed"

    ranked = client.get(f"/api/v1/assets/{asset['id']}/rankings").json()
    assert len(ranked) == 1
    assert ranked[0]["candidate_id"] == cand_id
    assert ranked[0]["keep"] is True


def test_search_endpoint_returns_indexed_chunks(client, asset, monkeypatch):
    # Pre-insert one embedding row by stuffing a 384-d zero vector.
    # Mock the query embedder so /search returns it regardless of text.
    import asyncio

    import numpy as np
    import sqlite_vec

    from ai_video_editor.database import get_db

    vec = np.zeros(384, dtype=np.float32)
    chunk_id = str(uuid.uuid4())

    async def _seed():
        db = await get_db()
        try:
            await db.execute(
                "INSERT INTO embeddings (chunk_id, video_id, start_seconds, "
                "end_seconds, text, vector) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    chunk_id,
                    asset["id"],
                    10.0,
                    35.0,
                    "let's go that was insane",
                    sqlite_vec.serialize_float32(vec.tolist()),
                ),
            )
            await db.commit()
        finally:
            await db.close()

    asyncio.run(_seed())

    # Make embed_texts deterministic so the test doesn't need fastembed.
    monkeypatch.setattr(
        "ai_video_editor.rag.embed_texts", lambda texts: [np.zeros(384, dtype=np.float32)]
    )

    hits = client.get("/api/v1/search", params={"q": "hype", "limit": 5}).json()
    assert hits and hits[0]["chunk_id"] == chunk_id
    assert hits[0]["text"].startswith("let's go")


def test_candidates_job_fails_for_unknown_asset(client):
    r = client.post(f"/api/v1/assets/{uuid.uuid4()}/candidates")
    # Routes raise 404 for unknown assets before scheduling any job.
    assert r.status_code == 404
