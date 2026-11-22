"""Unfiltered transcript readers see the mixed track only; owner rows are opt-in."""

from __future__ import annotations

import asyncio
import sqlite3
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

from ai_video_editor import rag
from ai_video_editor.config import settings
from ai_video_editor.database import get_db
from ai_video_editor.grokbot import voice

MIXED = "mixed words here"
OWNER = "owner only words"


def _seed(asset_id: str) -> None:
    async def _go():
        db = await get_db()
        try:
            for text, speaker, score in ((MIXED, None, 0.2), (OWNER, "owner", 0.9)):
                await db.execute(
                    "INSERT INTO transcripts (id, video_id, start_seconds, end_seconds, text, "
                    "sentiment_score, created_at, speaker) VALUES (?, ?, 5, 8, ?, ?, ?, ?)",
                    (
                        str(uuid.uuid4()),
                        asset_id,
                        text,
                        score,
                        datetime.now(timezone.utc).isoformat(),
                        speaker,
                    ),
                )
            await db.commit()
        finally:
            await db.close()

    asyncio.run(_go())


def test_get_transcript_route_excludes_owner(client, asset):
    _seed(asset["id"])
    body = client.get(f"/api/v1/assets/{asset['id']}/transcript").json()
    text = str(body)
    assert MIXED in text and OWNER not in text


def test_candidates_job_excludes_owner(client, asset, poll, monkeypatch):
    _seed(asset["id"])
    seen = {}

    def fake_compute(a, segments):
        seen["segments"] = segments
        return [], {}

    monkeypatch.setattr("ai_video_editor.routers.analyze.compute_candidates", fake_compute)
    job = poll(client, client.post(f"/api/v1/assets/{asset['id']}/candidates").json()["job_id"])
    assert job["status"] == "completed"
    assert [s["text"] for s in seen["segments"]] == [MIXED]


def test_caption_endpoint_excludes_owner(client, asset, poll, monkeypatch):
    _seed(asset["id"])
    seen = {}

    def fake_caption(*args, **kwargs):
        seen["segments"] = kwargs.get("segments", args[-3] if len(args) >= 4 else None)
        return (True, None)

    monkeypatch.setattr("ai_video_editor.routers.edits.apply_caption", fake_caption)
    payload = {"asset_id": asset["id"], "start_seconds": 0.0, "end_seconds": 15.0, "aspect": "9:16"}
    poll(client, client.post("/api/v1/edit/caption", json=payload).json()["job_id"])
    assert [s["text"] for s in seen["segments"]] == [MIXED]


def test_rag_index_excludes_owner(client, asset, monkeypatch):
    _seed(asset["id"])
    seen = {}

    def fake_chunk(segments, window, stride):
        seen["segments"] = segments
        return []

    monkeypatch.setattr(rag, "chunk_segments", fake_chunk)

    async def _go():
        db = await get_db()
        try:
            await rag.index_asset_transcript(db, asset["id"])
        finally:
            await db.close()

    asyncio.run(_go())
    assert [s["text"] for s in seen["segments"]] == [MIXED]


def test_window_sentiment_excludes_owner_and_slice_opts_in(client, asset):
    _seed(asset["id"])
    db_path = settings.database_path
    assert voice.window_sentiment(db_path, asset["id"], 0, 10) == 0.2
    assert voice.transcript_slice(db_path, asset["id"], 0, 10, speaker="owner") == OWNER


def test_transcript_slice_default_is_mixed_only(client, asset):
    _seed(asset["id"])
    assert voice.transcript_slice(settings.database_path, asset["id"], 0, 10) == MIXED


def test_owner_segment_gets_sentiment(monkeypatch, tmp_path):
    """transcribe_mic enriches owner segments with the same VADER score as the mixed path."""
    from ai_video_editor.candidates import mic_track
    from ai_video_editor.candidates.mic_track import AudioStream

    monkeypatch.setattr(settings, "workspace_dir", tmp_path)
    monkeypatch.setattr(settings, "mic_stream_override", None)
    monkeypatch.setattr(
        mic_track,
        "probe_audio_streams",
        lambda p: [AudioStream(0, "aac", 2, None, None), AudioStream(1, "aac", 2, "Mic", None)],
    )
    monkeypatch.setattr(mic_track, "_extract_audio", lambda p, w, stream_index=None: True)
    backend = SimpleNamespace(
        name="f",
        transcribe_raw=lambda p, **kw: [
            {"start_seconds": 0, "end_seconds": 1, "text": "NO WAY that was insane", "words": []}
        ],
    )
    monkeypatch.setattr(mic_track, "select_backend", lambda: backend)
    segs, _ = mic_track.transcribe_mic({"id": "a1", "path": "v.mp4"}, 60.0)
    assert isinstance(segs[0]["sentiment_score"], float)


def test_backend_failure_keeps_prior_owner_rows(client, asset, poll, monkeypatch):
    _seed(asset["id"])
    from ai_video_editor.candidates import mic_track

    monkeypatch.setattr(
        "ai_video_editor.routers.analyze.transcribe_mic",
        lambda a, d: ([], {"stream_index": 1, "method": "label", "activity": {}, "error": "boom"}),
    )
    monkeypatch.setattr("ai_video_editor.routers.analyze.get_duration_seconds", lambda p: 60.0)
    _ = mic_track
    job = poll(client, client.post(f"/api/v1/assets/{asset['id']}/transcribe-mic").json()["job_id"])
    assert job["status"] == "completed"
    c = sqlite3.connect(settings.database_path)
    n = c.execute("SELECT COUNT(*) FROM transcripts WHERE speaker='owner'").fetchone()[0]
    c.close()
    assert n == 1
