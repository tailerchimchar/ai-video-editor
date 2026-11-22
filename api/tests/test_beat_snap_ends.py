"""end_on_beat: only clip ENDS move to beats (fight windows keep their starts)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ai_video_editor.config import settings
from ai_video_editor.music.sync import ClipWindow, snap_clips_to_beats
from ai_video_editor.shorts import ShortClip, ShortPlan, render_short


def _w(start: float, end: float) -> ClipWindow:
    return ClipWindow(file="a.mp4", start_seconds=start, end_seconds=end, anchor_offset_seconds=1.0)


def test_ends_move_to_nearest_beat_starts_unchanged() -> None:
    # Montage ends: clip 1 at 10.0 -> beat 10.4; clip 2 at 10.4 + 9.9 = 20.3 -> beat 20.0.
    snapped = snap_clips_to_beats(
        [_w(0.0, 10.0), _w(0.0, 9.9)], (10.4, 20.0), strategy="end_on_beat"
    )
    first, second = snapped.clips
    assert first.new_end_seconds == pytest.approx(10.4)
    assert second.new_end_seconds == pytest.approx(9.6)  # montage end 20.0
    assert first.new_start_seconds == 0.0 and second.new_start_seconds == 0.0
    assert snapped.strategy == "end_on_beat"


def test_beat_beyond_tolerance_leaves_clip_unchanged() -> None:
    snapped = snap_clips_to_beats([_w(5.0, 10.0)], (10.6,), strategy="end_on_beat")
    assert snapped.clips[0].new_end_seconds == 5.0 + 5.0
    assert snapped.clips[0].shift_seconds == 0.0


def test_never_shrinks_clip_below_half_second() -> None:
    snapped = snap_clips_to_beats([_w(0.0, 0.8)], (0.4,), strategy="end_on_beat")
    assert snapped.clips[0].new_end_seconds == 0.8


# ----- render_short wiring (ffmpeg stubbed) ---------------------------------

_BEATS = tuple(i * 0.5 for i in range(200))


def _clip(start: float, end: float, anchor: float | None) -> ShortClip:
    return ShortClip(
        file="a.mp4",
        path=Path("/tmp/a.mp4"),
        event_type="kill",
        start_seconds=start,
        end_seconds=end,
        hype_score=0.9,
        funny_score=0.5,
        story_score=0.5,
        reason="",
        anchor_offset_seconds=anchor,
    )


def _render(clip: ShortClip, **kwargs) -> list[str]:
    captured: list[list[str]] = []

    def fake_run(cmd, **_):
        captured.append(cmd)
        return SimpleNamespace(returncode=0, stderr="")

    analysis = SimpleNamespace(beats=SimpleNamespace(beats=_BEATS))
    plan = ShortPlan(bucket="custom", clips=(clip,), title="T", vo_prompt="", index=1)
    with (
        patch.object(subprocess, "run", fake_run),
        patch("ai_video_editor.music.analysis.load_or_analyze", return_value=analysis),
    ):
        ok, err = render_short(
            plan, Path("out") / "s.mp4", mode="montage", music_path="s.mp3", beat_flash=False, **kwargs
        )
    assert ok, err
    return captured[0]


def _val(cmd: list[str], flag: str) -> float:
    return float(cmd[cmd.index(flag) + 1])


def test_setting_on_snaps_end_but_not_start(monkeypatch) -> None:
    monkeypatch.setattr(settings, "shorts_beat_sync_enabled", True)
    cmd = _render(_clip(10.0, 20.2, 2.0))  # montage end 10.2 -> beat 10.0
    assert _val(cmd, "-ss") == 10.0
    assert _val(cmd, "-t") == pytest.approx(10.0)


def test_setting_off_is_unsnapped(monkeypatch) -> None:
    monkeypatch.setattr(settings, "shorts_beat_sync_enabled", False)
    cmd = _render(_clip(10.0, 20.2, 2.0))
    assert _val(cmd, "-t") == pytest.approx(10.2)
