"""Beat-flash source selection tests. Pure — no ffmpeg."""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_video_editor.shorts import (
    ShortClip,
    ShortPlan,
    _flash_times,
    _kill_times_in_montage,
)


def _clip(file: str, start: float, end: float) -> ShortClip:
    return ShortClip(
        file=file,
        path=Path("/tmp") / file,
        event_type="kill",
        start_seconds=start,
        end_seconds=end,
        hype_score=0.9,
        funny_score=0.5,
        story_score=0.5,
        reason="",
    )


def _plan(clips: list[ShortClip]) -> ShortPlan:
    return ShortPlan(
        bucket="multikill",
        clips=tuple(clips),
        title="TEST",
        vo_prompt="",
        index=1,
    )


def test_kill_times_single_clip_at_midpoint() -> None:
    """A 10s clip's kill should fire at t=5s in the montage."""
    plan = _plan([_clip("a.mp4", 100.0, 110.0)])
    times = _kill_times_in_montage(plan)
    assert times == (5.0,)


def test_kill_times_three_clips_concat_at_midpoints() -> None:
    """Three 10s clips: kill moments at 5, 15, 25 in montage time."""
    plan = _plan(
        [
            _clip("a.mp4", 0.0, 10.0),
            _clip("b.mp4", 0.0, 10.0),
            _clip("c.mp4", 0.0, 10.0),
        ]
    )
    assert _kill_times_in_montage(plan) == (5.0, 15.0, 25.0)


def test_kill_times_varying_durations() -> None:
    """5s clip then 20s clip: kills at 2.5 and 5+10=15."""
    plan = _plan(
        [
            _clip("short.mp4", 0.0, 5.0),
            _clip("long.mp4", 0.0, 20.0),
        ]
    )
    assert _kill_times_in_montage(plan) == (2.5, 15.0)


def test_flash_times_kills_needs_no_music(tmp_path) -> None:
    """`kills` source works without any music file."""
    plan = _plan([_clip("a.mp4", 0.0, 10.0)])
    times = _flash_times(plan, music_path="", source="kills", max_t=60.0)
    assert times == (5.0,)


def test_flash_times_kills_clips_by_max_t() -> None:
    """Kills past max_t are dropped (prevents flashing on frames the
    final 60s cap won't include)."""
    plan = _plan(
        [
            _clip("a.mp4", 0.0, 40.0),  # kill at t=20
            _clip("b.mp4", 0.0, 40.0),  # kill at t=60
            _clip("c.mp4", 0.0, 40.0),  # kill at t=100 — past cap
        ]
    )
    times = _flash_times(plan, music_path="", source="kills", max_t=60.0)
    assert times == (20.0, 60.0)


def test_flash_times_unknown_source_raises(tmp_path) -> None:
    plan = _plan([_clip("a.mp4", 0.0, 10.0)])
    with pytest.raises(ValueError, match="unknown beat_flash_source"):
        _flash_times(plan, music_path="/tmp/x.mp3", source="drops", max_t=60.0)


def test_flash_times_downbeats_takes_every_fourth(monkeypatch, tmp_path) -> None:
    """downbeats source thins the beat list to every 4th onset."""
    from ai_video_editor.music import beats as beats_mod

    mp3 = tmp_path / "song.mp3"
    mp3.write_bytes(b"fake")

    def _fake_detector(mp3_path: Path):
        return (120.0, [0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5])

    monkeypatch.setitem(beats_mod._BEAT_DETECTORS, "librosa", _fake_detector)
    plan = _plan([_clip("a.mp4", 0.0, 10.0)])
    times = _flash_times(
        plan, music_path=str(mp3), source="downbeats", max_t=60.0
    )
    assert times == (0.0, 2.0)  # every 4th of 8 quarter-notes


def test_flash_times_beats_takes_all(monkeypatch, tmp_path) -> None:
    """beats source uses every detected beat."""
    from ai_video_editor.music import beats as beats_mod

    mp3 = tmp_path / "song.mp3"
    mp3.write_bytes(b"fake")

    def _fake_detector(mp3_path: Path):
        return (120.0, [0.5, 1.0, 1.5])

    monkeypatch.setitem(beats_mod._BEAT_DETECTORS, "librosa", _fake_detector)
    plan = _plan([_clip("a.mp4", 0.0, 10.0)])
    times = _flash_times(plan, music_path=str(mp3), source="beats", max_t=60.0)
    assert times == (0.5, 1.0, 1.5)
