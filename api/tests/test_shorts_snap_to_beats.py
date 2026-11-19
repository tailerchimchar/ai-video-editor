"""render_short(snap_to_beats=...): cuts move to beats only when asked. ffmpeg is stubbed."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ai_video_editor import shorts
from ai_video_editor.shorts import ShortClip, ShortPlan, render_short

_BEATS = tuple(i * 0.5 for i in range(200))


def _clip(file: str, start: float, end: float, anchor: float | None) -> ShortClip:
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
        anchor_offset_seconds=anchor,
    )


def _plan(*clips: ShortClip) -> ShortPlan:
    return ShortPlan(bucket="custom", clips=clips, title="T", vo_prompt="", index=1)


def _render(plan: ShortPlan, analysis_error: Exception | None = None, **kwargs) -> list[str]:
    """Run render_short with ffmpeg and beat analysis stubbed; return the ffmpeg argv."""
    captured: list[list[str]] = []

    def fake_run(cmd, **_):
        captured.append(cmd)
        return SimpleNamespace(returncode=0, stderr="")

    analysis = SimpleNamespace(beats=SimpleNamespace(beats=_BEATS))
    with (
        patch.object(subprocess, "run", fake_run),
        patch(
            "ai_video_editor.music.analysis.load_or_analyze",
            return_value=analysis,
            side_effect=analysis_error,
        ),
    ):
        ok, err = render_short(
            plan,
            Path("out") / "s.mp4",
            mode="montage",
            music_path="song.mp3",
            beat_flash=False,
            **kwargs,
        )
    assert ok, err
    return captured[0]


def _ss_values(cmd: list[str]) -> list[float]:
    return [float(cmd[i + 1]) for i, a in enumerate(cmd) if a == "-ss"]


@pytest.fixture
def anchored_plan() -> ShortPlan:
    # Anchor 1.2s into the clip; nearest beat is 1.0, so the start moves +0.2s.
    return _plan(_clip("a.mp4", 10.0, 20.0, 1.2))


def test_default_leaves_cut_points_untouched(anchored_plan) -> None:
    assert _ss_values(_render(anchored_plan)) == [10.0]


def test_false_is_byte_identical_to_default(anchored_plan) -> None:
    assert _render(anchored_plan, snap_to_beats=False) == _render(anchored_plan)


def test_true_moves_cut_to_nearest_beat_within_tolerance(anchored_plan) -> None:
    assert _ss_values(_render(anchored_plan, snap_to_beats=True)) == [pytest.approx(10.2)]


def test_true_never_shifts_more_than_tolerance() -> None:
    plan = _plan(_clip("a.mp4", 10.0, 20.0, 1.2))
    with patch.object(shorts, "_SNAP_TOLERANCE_SECONDS", 0.1):
        assert _ss_values(_render(plan, snap_to_beats=True)) == [pytest.approx(10.1)]


def test_unanchored_clip_is_left_alone() -> None:
    plan = _plan(_clip("a.mp4", 10.0, 20.0, None))
    assert "-ss" not in _render(plan, snap_to_beats=True)


def test_beat_analysis_failure_falls_back_to_unsnapped(anchored_plan) -> None:
    cmd = _render(anchored_plan, analysis_error=OSError("boom"), snap_to_beats=True)
    assert _ss_values(cmd) == [10.0]
