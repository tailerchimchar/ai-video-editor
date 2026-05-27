"""Thumbnail extractor — clip-pick heuristic, midpoint math, ffmpeg call."""

from __future__ import annotations

from pathlib import Path

from ai_video_editor.thumbnail import (
    _pick_thumbnail_clip,
    _reel_midpoint,
    extract_thumbnail,
    safe_extract_thumbnail,
    thumbnail_path,
)


def _clip(cid: str, hype: float, start=0.0, end=10.0, kind="clip") -> dict:
    return {
        "id": cid,
        "event_type": kind,
        "hype_score": hype,
        "start_seconds": start,
        "end_seconds": end,
    }


def _spec(*clips) -> dict:
    return {"clips": list(clips)}


# ----- clip picking heuristic -----


def test_picks_highest_hype_clip():
    spec = _spec(_clip("a", hype=0.3), _clip("b", hype=0.8), _clip("c", hype=0.5))
    chosen = _pick_thumbnail_clip(spec)
    assert chosen["id"] == "b"


def test_skips_intro_clips():
    """Intro clips are branding, not gameplay — should never be the thumbnail."""
    spec = _spec(
        _clip("intro", hype=0.0, kind="intro"),
        _clip("game1", hype=0.2),
        _clip("game2", hype=0.5),
    )
    chosen = _pick_thumbnail_clip(spec)
    assert chosen["id"] == "game2"


def test_returns_none_when_only_intro():
    spec = _spec(_clip("i", hype=0.0, kind="intro"))
    assert _pick_thumbnail_clip(spec) is None


def test_returns_none_for_empty_spec():
    assert _pick_thumbnail_clip({"clips": []}) is None
    assert _pick_thumbnail_clip({}) is None


def test_tie_break_prefers_earlier_clip():
    """When two clips tie on hype, the earlier one wins — usually the
    hook-ordered first non-intro clip, which is the right thumbnail."""
    spec = _spec(_clip("first", hype=0.5, start=10), _clip("later", hype=0.5, start=100))
    assert _pick_thumbnail_clip(spec)["id"] == "first"


# ----- midpoint math -----


def test_midpoint_first_clip():
    """Midpoint of clip[0] = duration/2 (assuming no preceding clips)."""
    c = _clip("a", hype=1.0, start=0, end=10)
    spec = _spec(c)
    assert _reel_midpoint(spec, c) == 5.0


def test_midpoint_after_intro():
    """A 3s intro followed by a 10s clip — midpoint of the clip is at
    3 + 5 = 8s reel time."""
    intro = _clip("i", hype=0.0, start=0, end=3, kind="intro")
    game = _clip("g", hype=0.8, start=0, end=10)
    spec = _spec(intro, game)
    assert _reel_midpoint(spec, game) == 8.0


def test_midpoint_for_missing_clip_falls_back_to_reel_middle():
    """A clip not in the spec (defensive) returns half the running
    total — coarse but harmless."""
    c1 = _clip("a", hype=0.5, start=0, end=4)
    c2 = _clip("b", hype=0.5, start=0, end=6)
    spec = _spec(c1, c2)
    phantom = _clip("phantom", hype=1.0, start=0, end=20)
    # Running total = 4 + 6 = 10. Fallback midpoint = 5.
    assert _reel_midpoint(spec, phantom) == 5.0


# ----- extract_thumbnail (ffmpeg mocked) -----


def test_extract_returns_no_match_when_no_video(tmp_path: Path):
    spec = _spec(_clip("a", hype=0.5))
    result = extract_thumbnail(tmp_path, spec, tmp_path / "missing.mp4")
    assert result["ok"] is False
    assert result["reason"] == "video_not_found"


def test_extract_returns_no_gameplay_when_only_intros(tmp_path: Path):
    """Even with a valid video, an all-intro reel can't pick a thumbnail."""
    video = tmp_path / "v.mp4"
    video.write_bytes(b"\x00")
    spec = _spec(_clip("i", hype=0, kind="intro"))
    result = extract_thumbnail(tmp_path, spec, video)
    assert result["ok"] is False
    assert result["reason"] == "no_gameplay_clips"


def test_extract_invokes_ffmpeg_with_seek_and_jpeg_output(tmp_path: Path, monkeypatch):
    from ai_video_editor import thumbnail as thumb_mod

    video = tmp_path / "v.mp4"
    video.write_bytes(b"\x00")
    spec = _spec(
        _clip("intro", hype=0, start=0, end=3, kind="intro"),
        _clip("game1", hype=0.9, start=0, end=10),
    )

    captured: dict = {}

    def fake_run(cmd, capture_output=True, text=True):
        captured["cmd"] = cmd

        class R:
            returncode = 0
            stderr = ""

        return R()

    monkeypatch.setattr(thumb_mod.subprocess, "run", fake_run)
    result = extract_thumbnail(tmp_path, spec, video)
    assert result["ok"] is True
    assert result["path"].endswith("thumbnail.jpg")
    assert result["source_clip_id"] == "game1"
    cmd = captured["cmd"]
    # Seek to clip-midpoint = 3 (intro) + 5 (clip midpoint) = 8 reel-time
    ss_idx = cmd.index("-ss")
    assert cmd[ss_idx + 1] == "8.000"
    # Single-frame extraction at high quality
    assert "-frames:v" in cmd
    assert cmd[cmd.index("-frames:v") + 1] == "1"
    assert "-q:v" in cmd


def test_extract_propagates_ffmpeg_failure(tmp_path: Path, monkeypatch):
    from ai_video_editor import thumbnail as thumb_mod

    video = tmp_path / "v.mp4"
    video.write_bytes(b"\x00")

    def fake_run(cmd, capture_output=True, text=True):
        class R:
            returncode = 1
            stderr = "ffmpeg error tail here"

        return R()

    monkeypatch.setattr(thumb_mod.subprocess, "run", fake_run)
    result = extract_thumbnail(tmp_path, _spec(_clip("a", hype=0.5)), video)
    assert result["ok"] is False
    assert result["reason"] == "ffmpeg_failed"
    assert "error tail here" in result["error"]


def test_safe_extract_swallows_exception(tmp_path: Path, monkeypatch):
    """Any unexpected exception inside extract_thumbnail must become
    a structured failure — render_spec mustn't fail because thumbnail
    extraction threw."""
    from ai_video_editor import thumbnail as thumb_mod

    def boom(*a, **kw):
        raise RuntimeError("oops")

    monkeypatch.setattr(thumb_mod, "extract_thumbnail", boom)
    result = safe_extract_thumbnail(tmp_path, {"clips": []}, tmp_path / "v.mp4")
    assert result["ok"] is False
    assert result["reason"] == "exception"
    assert "oops" in result["error"]


def test_thumbnail_path_is_in_folder(tmp_path: Path):
    assert thumbnail_path(tmp_path).name == "thumbnail.jpg"
    assert thumbnail_path(tmp_path).parent == tmp_path
