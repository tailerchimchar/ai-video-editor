"""Outplayed direct-compile tests. Pure filesystem scans — no ffmpeg."""

from __future__ import annotations

from datetime import datetime

from ai_video_editor.shorts import (
    OutplayedClipRef,
    _filter_by_since,
    _scan_outplayed_clips,
)


def _make_mp4(path, size_bytes: int = 50 * 1024 * 1024) -> None:
    """Write a fake mp4 of the given size (0-fill)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"\0" * size_bytes)


def test_scan_returns_empty_when_root_missing(tmp_path, monkeypatch) -> None:
    from ai_video_editor.config import settings

    monkeypatch.setattr(settings, "outplayed_media_dir", tmp_path)
    result = _scan_outplayed_clips("MissingGame")
    assert result == []


def test_scan_skips_files_bigger_than_prefilter(tmp_path, monkeypatch) -> None:
    """A > 500MB file is a VOD, not a clip — skip without ffprobe."""
    from ai_video_editor.config import settings
    from ai_video_editor.shorts import _scan_outplayed_clips as scan

    monkeypatch.setattr(settings, "outplayed_media_dir", tmp_path)
    game_dir = tmp_path / "Valorant" / "Valorant_07-07-2026_20-00-0"
    # 1MB fake clip; 501MB fake "VOD"
    _make_mp4(game_dir / "clip_a.mp4", size_bytes=1 * 1024 * 1024)
    _make_mp4(game_dir / "vod_b.mp4", size_bytes=501 * 1024 * 1024)

    # Stub ffprobe so we can count how often it fires — VOD should never be probed
    calls: list[str] = []

    def _fake_duration(path):
        calls.append(path)
        return 60.0

    monkeypatch.setattr(
        "ai_video_editor.candidates.probe.get_duration_seconds",
        _fake_duration,
    )
    result = scan("Valorant")
    assert len(result) == 1
    assert result[0].path.name == "clip_a.mp4"
    # VOD path never made it to ffprobe
    assert not any("vod_b" in c for c in calls)


def test_scan_drops_clips_over_max_duration(tmp_path, monkeypatch) -> None:
    """Duration filter: > outplayed_clip_max_seconds means it's a VOD."""
    from ai_video_editor.config import settings

    monkeypatch.setattr(settings, "outplayed_media_dir", tmp_path)
    game_dir = tmp_path / "Valorant" / "session_1"
    _make_mp4(game_dir / "a.mp4", size_bytes=50 * 1024 * 1024)
    _make_mp4(game_dir / "b.mp4", size_bytes=50 * 1024 * 1024)

    def _fake_duration(path):
        # a.mp4 = 45s (real clip); b.mp4 = 300s (long)
        return 45.0 if "a.mp4" in path else 300.0

    monkeypatch.setattr(
        "ai_video_editor.candidates.probe.get_duration_seconds",
        _fake_duration,
    )
    result = _scan_outplayed_clips("Valorant", max_duration_seconds=120.0)
    files = {r.path.name for r in result}
    assert files == {"a.mp4"}


def test_scan_sorts_by_mtime_desc(tmp_path, monkeypatch) -> None:
    """Most recent clip first."""
    from ai_video_editor.config import settings

    monkeypatch.setattr(settings, "outplayed_media_dir", tmp_path)
    game_dir = tmp_path / "Valorant" / "s1"
    old = game_dir / "old.mp4"
    new = game_dir / "new.mp4"
    _make_mp4(old, size_bytes=1024)
    _make_mp4(new, size_bytes=1024)
    # Force distinct mtimes
    import os
    now = 1_800_000_000  # arbitrary epoch
    os.utime(old, (now - 3600, now - 3600))
    os.utime(new, (now, now))

    monkeypatch.setattr(
        "ai_video_editor.candidates.probe.get_duration_seconds",
        lambda p: 30.0,
    )
    result = _scan_outplayed_clips("Valorant")
    assert [r.path.name for r in result] == ["new.mp4", "old.mp4"]


def _ref(name: str, hours_ago: float, now_epoch: float) -> OutplayedClipRef:
    from pathlib import Path

    return OutplayedClipRef(
        path=Path("/tmp") / name,
        duration_seconds=30.0,
        mtime_epoch=now_epoch - hours_ago * 3600,
    )


def test_filter_by_since_none_returns_all() -> None:
    now = datetime(2026, 7, 7, 20, 0, 0)
    clips = [
        _ref("a.mp4", 24, now.timestamp()),
        _ref("b.mp4", 240, now.timestamp()),
    ]
    result = _filter_by_since(clips, None, now)
    assert len(result) == 2


def test_filter_by_since_one_day_keeps_recent() -> None:
    now = datetime(2026, 7, 7, 20, 0, 0)
    clips = [
        _ref("today.mp4", 2, now.timestamp()),      # 2h ago — kept
        _ref("yesterday.mp4", 20, now.timestamp()), # 20h ago — kept (<24h)
        _ref("old.mp4", 240, now.timestamp()),      # 10 days ago — dropped
    ]
    result = _filter_by_since(clips, since_days=1, now=now)
    files = {r.path.name for r in result}
    assert files == {"today.mp4", "yesterday.mp4"}


def test_filter_by_since_seven_days_boundary() -> None:
    now = datetime(2026, 7, 7, 20, 0, 0)
    clips = [
        _ref("edge_in.mp4", 24 * 6, now.timestamp()),  # 6 days ago — in
        _ref("edge_out.mp4", 24 * 8, now.timestamp()), # 8 days ago — out
    ]
    result = _filter_by_since(clips, since_days=7, now=now)
    files = {r.path.name for r in result}
    assert files == {"edge_in.mp4"}
