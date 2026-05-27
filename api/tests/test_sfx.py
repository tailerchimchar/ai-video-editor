"""Sprint #2 — SFX extraction endpoint.

Real ffmpeg is mocked; we verify routing, path resolution, profile
integration, and job lifecycle.
"""

from __future__ import annotations

import uuid

import pytest


@pytest.fixture(autouse=True)
def _clear_profile_cache():
    from ai_video_editor.profiles import _registry

    _registry.cache_clear()
    yield
    _registry.cache_clear()


def _mock_extract_ok(monkeypatch):
    """Patch sfx.extract_sfx to succeed without invoking ffmpeg, writing
    a stub file at the requested output path so the assertion that
    'the file lives where we expect' is meaningful."""
    from pathlib import Path

    def fake(asset_path, output_path, start, end):
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_bytes(b"RIFF\x00\x00\x00\x00WAVE")
        return True, None

    monkeypatch.setattr("ai_video_editor.routers.sfx.extract_sfx", fake)


def test_extract_400s_when_end_le_start(client, asset):
    r = client.post(
        "/api/v1/sfx/extract",
        json={
            "asset_id": asset["id"],
            "game": "league",
            "sound_name": "first_blood",
            "start_seconds": 10.0,
            "end_seconds": 10.0,
        },
    )
    assert r.status_code == 400


def test_extract_404s_unknown_asset(client):
    r = client.post(
        "/api/v1/sfx/extract",
        json={
            "asset_id": str(uuid.uuid4()),
            "game": "league",
            "sound_name": "first_blood",
            "start_seconds": 0.0,
            "end_seconds": 2.0,
        },
    )
    assert r.status_code == 404


def test_extract_writes_to_canonical_game_dir(client, asset, poll, monkeypatch):
    """Profile alias 'League of Legends' must resolve to the canonical
    'league' directory under media_library/."""
    from ai_video_editor.config import settings

    _mock_extract_ok(monkeypatch)
    r = client.post(
        "/api/v1/sfx/extract",
        json={
            "asset_id": asset["id"],
            "game": "League of Legends",
            "sound_name": "first_blood",
            "start_seconds": 4.0,
            "end_seconds": 6.0,
        },
    ).json()
    job = poll(client, r["job_id"])
    assert job["status"] == "completed"

    expected = settings.workspace_dir / "media_library" / "league" / "sfx" / "first_blood.wav"
    assert expected.exists(), f"file should land at {expected}"
    assert expected.as_posix() in (job.get("output_path") or "")


def test_extract_warns_when_sound_name_not_declared(client, asset, poll, monkeypatch):
    """An undeclared sound is still saved (so users can extract ad-hoc),
    but the job output flags a warning so the caller knows to add the
    entry to the profile for tools downstream to pick it up."""
    _mock_extract_ok(monkeypatch)
    r = client.post(
        "/api/v1/sfx/extract",
        json={
            "asset_id": asset["id"],
            "game": "league",
            "sound_name": "some_new_meme",
            "start_seconds": 1.0,
            "end_seconds": 3.0,
        },
    ).json()
    job = poll(client, r["job_id"])
    assert job["status"] == "completed"
    out = job.get("output_path") or ""
    assert "warning" in out.lower()
    assert "some_new_meme" in out


def test_extract_unknown_game_falls_back_to_default_dir(client, asset, poll, monkeypatch):
    """An unknown game routes through the default profile (no sounds),
    so the file lands in media_library/default/sfx/ and the warning
    fires."""
    from ai_video_editor.config import settings

    _mock_extract_ok(monkeypatch)
    r = client.post(
        "/api/v1/sfx/extract",
        json={
            "asset_id": asset["id"],
            "game": "Bowling",
            "sound_name": "strike",
            "start_seconds": 0.0,
            "end_seconds": 1.5,
        },
    ).json()
    job = poll(client, r["job_id"])
    assert job["status"] == "completed"
    expected = settings.workspace_dir / "media_library" / "default" / "sfx" / "strike.wav"
    assert expected.exists()


def test_extract_persists_job_row_with_sfx_type(client, asset, monkeypatch):
    """Sanity: the job row carries `sfx_extract` type so it's
    distinguishable in /jobs listings."""
    import asyncio

    from ai_video_editor.database import get_db

    _mock_extract_ok(monkeypatch)
    r = client.post(
        "/api/v1/sfx/extract",
        json={
            "asset_id": asset["id"],
            "game": "league",
            "sound_name": "ace",
            "start_seconds": 0.0,
            "end_seconds": 1.0,
        },
    ).json()
    job_id = r["job_id"]

    async def _row():
        db = await get_db()
        try:
            rows = await db.execute_fetchall("SELECT type FROM jobs WHERE id = ?", (job_id,))
            return dict(rows[0])["type"]
        finally:
            await db.close()

    assert asyncio.run(_row()) == "sfx_extract"


def test_extract_failure_records_error(client, asset, poll, monkeypatch):
    """ffmpeg failure should land in the job's error field."""

    def fake_fail(asset_path, output_path, start, end):
        return False, "ffmpeg exploded"

    monkeypatch.setattr("ai_video_editor.routers.sfx.extract_sfx", fake_fail)
    r = client.post(
        "/api/v1/sfx/extract",
        json={
            "asset_id": asset["id"],
            "game": "league",
            "sound_name": "first_blood",
            "start_seconds": 0.0,
            "end_seconds": 2.0,
        },
    ).json()
    job = poll(client, r["job_id"])
    assert job["status"] == "failed"
    assert "ffmpeg exploded" in (job.get("error") or "")
