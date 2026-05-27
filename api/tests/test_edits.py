"""Edit primitives — unit tests on the pure filter/ROI/caption builders
plus integration tests on the three endpoints with ffmpeg mocked out."""

import uuid
from datetime import datetime, timezone

import pytest

from ai_video_editor.edits import (
    _escape_drawtext,
    apply_caption,
    apply_focus,
    apply_zoom,
    aspect_filter,
    caption_filters,
    resolve_roi,
)

# ----- pure unit tests -----


def test_aspect_filter_passthrough_vs_vertical():
    assert aspect_filter("16:9") == ""
    v = aspect_filter("9:16")
    assert "crop=ih*9/16:ih" in v and "scale=720:1280" in v


def test_resolve_roi_center_substitutes_factor():
    w, h, x, y = resolve_roi("center", 2.0)
    assert w == "iw/2.0" and h == "ih/2.0"
    assert x == "(iw-iw/2.0)/2" and y == "(ih-ih/2.0)/2"


def test_resolve_roi_explicit_box_fractions():
    w, h, x, y = resolve_roi({"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4}, factor=1.0)
    assert w == "iw*0.3" and h == "ih*0.4" and x == "iw*0.1" and y == "ih*0.2"


def test_resolve_roi_unknown_preset_raises():
    with pytest.raises(ValueError):
        resolve_roi("not_a_preset", 2.0)


def test_resolve_roi_preset_scoreline_returns_expressions():
    w, h, x, y = resolve_roi("scoreline_lol", 2.0)
    # Should be ffmpeg-readable expressions referencing iw/ih
    for part in (w, h, x, y):
        assert "iw" in part or "ih" in part or part == "0"


def test_escape_drawtext_handles_colons_and_quotes():
    assert ":" not in _escape_drawtext("12:34").replace(r"\:", "")
    # Apostrophes are replaced with the curly variant (U+2019) so they
    # don't terminate ffmpeg's single-quoted drawtext value.
    out = _escape_drawtext("let's go")
    assert "'" not in out and "’" in out  # noqa: RUF001 (intentional curly)


def test_caption_filters_subtracts_clip_offset_and_chains():
    # A segment at VOD 100..103, clip starts at VOD 95 → drawtext fires
    # at t=5..8 relative to the clip.
    segs = [
        {"start_seconds": 100.0, "end_seconds": 103.0, "text": "let's go"},
        {"start_seconds": 110.0, "end_seconds": 111.0, "text": "insane"},
    ]
    filt = caption_filters(segs, clip_start_offset=95.0)
    assert filt.count("drawtext=") == 2
    assert "between(t,5.00,8.00)" in filt
    assert "between(t,15.00,16.00)" in filt
    # Stroke + center align baked in
    assert "borderw=4:bordercolor=black" in filt
    assert "x=(w-text_w)/2" in filt


def test_caption_filters_empty_segments_returns_empty_string():
    assert caption_filters([], clip_start_offset=0) == ""


# ----- integration tests on the endpoints (ffmpeg is monkeypatched) -----


def _ok_ffmpeg(monkeypatch):
    """Stub the three apply_* funcs to "succeed" without running ffmpeg."""
    monkeypatch.setattr("ai_video_editor.routers.edits.apply_zoom", lambda *a, **k: (True, None))
    monkeypatch.setattr("ai_video_editor.routers.edits.apply_caption", lambda *a, **k: (True, None))
    monkeypatch.setattr("ai_video_editor.routers.edits.apply_focus", lambda *a, **k: (True, None))


def test_zoom_endpoint_persists_edit_row(client, asset, poll, monkeypatch):
    _ok_ffmpeg(monkeypatch)
    payload = {
        "asset_id": asset["id"],
        "start_seconds": 0.0,
        "end_seconds": 15.0,
        "factor": 2.0,
        "roi": "scoreline_lol",
        "aspect": "16:9",
    }
    r = client.post("/api/v1/edit/zoom", json=payload).json()
    job = poll(client, r["job_id"])
    assert job["status"] == "completed"
    assert job["output_path"].endswith(".mp4")


def test_caption_endpoint_auto_pulls_transcript(client, asset, poll, monkeypatch):
    # Seed one transcript segment overlapping the requested window so
    # the endpoint's auto path has something to draw.
    import asyncio

    from ai_video_editor.database import get_db

    async def _seed():
        db = await get_db()
        try:
            await db.execute(
                "INSERT INTO transcripts (id, video_id, start_seconds, end_seconds, text, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    str(uuid.uuid4()),
                    asset["id"],
                    5.0,
                    8.0,
                    "let's go that was insane",
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            await db.commit()
        finally:
            await db.close()

    asyncio.run(_seed())

    captured: dict = {}

    def _capture_caption(*args, **kwargs):
        captured["segments"] = kwargs.get("segments", args[-3] if len(args) >= 4 else None)
        return (True, None)

    monkeypatch.setattr("ai_video_editor.routers.edits.apply_caption", _capture_caption)

    payload = {
        "asset_id": asset["id"],
        "start_seconds": 0.0,
        "end_seconds": 15.0,
        "aspect": "9:16",
    }
    r = client.post("/api/v1/edit/caption", json=payload).json()
    poll(client, r["job_id"])

    # The endpoint should have pulled our seeded segment from the DB.
    assert captured["segments"], "auto-caption did not pull transcript segments"
    assert "insane" in captured["segments"][0]["text"]


def test_focus_endpoint_404s_unknown_asset(client):
    r = client.post(
        "/api/v1/edit/focus",
        json={"asset_id": str(uuid.uuid4()), "start_seconds": 0.0, "end_seconds": 5.0},
    )
    assert r.status_code == 404


def test_get_edit_returns_persisted_row(client, asset, poll, monkeypatch):
    _ok_ffmpeg(monkeypatch)
    r = client.post(
        "/api/v1/edit/zoom",
        json={"asset_id": asset["id"], "start_seconds": 0.0, "end_seconds": 10.0},
    ).json()
    poll(client, r["job_id"])
    # Find the edit row via the DB to get its id (endpoint doesn't return it currently)
    import asyncio

    from ai_video_editor.database import get_db

    async def _find():
        db = await get_db()
        try:
            rows = await db.execute_fetchall(
                "SELECT id FROM edits WHERE asset_id = ?", (asset["id"],)
            )
            return [dict(x) for x in rows]
        finally:
            await db.close()

    edits = asyncio.run(_find())
    assert edits, "edit row was not persisted"
    body = client.get(f"/api/v1/edit/{edits[0]['id']}").json()
    assert body["kind"] == "zoom"
    assert isinstance(body["params"], dict)


# Light sanity checks on the public apply_* wrappers — they only need
# ffmpeg's CLI to be missing for the FileNotFoundError branch.
def test_apply_zoom_returns_error_when_ffmpeg_missing(monkeypatch, tmp_path):
    monkeypatch.setattr("ai_video_editor.config.settings.ffmpeg_path", "/nonexistent/ffmpeg-bin")
    ok, err = apply_zoom(
        "input.mp4",
        str(tmp_path / "out.mp4"),
        start=0.0,
        end=1.0,
        factor=2.0,
        roi="center",
        aspect="16:9",
    )
    assert ok is False and "ffmpeg" in err.lower()


def test_apply_caption_returns_error_when_ffmpeg_missing(monkeypatch, tmp_path):
    monkeypatch.setattr("ai_video_editor.config.settings.ffmpeg_path", "/nonexistent/ffmpeg-bin")
    ok, _ = apply_caption(
        "input.mp4",
        str(tmp_path / "out.mp4"),
        start=0.0,
        end=1.0,
        segments=[],
        aspect="16:9",
    )
    assert ok is False


def test_apply_focus_returns_error_when_ffmpeg_missing(monkeypatch, tmp_path):
    monkeypatch.setattr("ai_video_editor.config.settings.ffmpeg_path", "/nonexistent/ffmpeg-bin")
    ok, _ = apply_focus(
        "input.mp4",
        str(tmp_path / "out.mp4"),
        start=0.0,
        end=1.0,
        x_frac=0.5,
        y_frac=0.5,
        r_frac=0.2,
        dim=0.3,
        aspect="16:9",
    )
    assert ok is False
