"""Sample-time planner tests.

Pure math — no ffmpeg. The extract_frames path is exercised in the
end-to-end smoke test (task #146), not here.
"""

from __future__ import annotations

import pytest

from ai_video_editor.vlm.frames import plan_sample_times


def test_plan_sample_times_zero_samples() -> None:
    assert plan_sample_times(10.0, 0) == []


def test_plan_sample_times_zero_duration() -> None:
    assert plan_sample_times(0.0, 4) == []


def test_plan_sample_times_negative_duration_returns_empty() -> None:
    assert plan_sample_times(-5.0, 4) == []


def test_plan_sample_times_single_sample_uses_midpoint() -> None:
    assert plan_sample_times(10.0, 1) == [5.0]


def test_plan_sample_times_endpoints_avoided() -> None:
    # First sample past 0.0, last before duration
    times = plan_sample_times(10.0, 4)
    assert times[0] > 0.0
    assert times[-1] < 10.0


def test_plan_sample_times_evenly_spaced() -> None:
    times = plan_sample_times(10.0, 5)
    diffs = [round(times[i + 1] - times[i], 3) for i in range(len(times) - 1)]
    assert len(set(diffs)) == 1, f"non-uniform spacing: {diffs}"


def test_plan_sample_times_boundaries_5_and_95_percent() -> None:
    # 20s clip, 2 samples → first at 1.0, last at 19.0
    times = plan_sample_times(20.0, 2)
    assert times[0] == pytest.approx(1.0)
    assert times[-1] == pytest.approx(19.0)


def test_plan_sample_times_very_short_clip_stays_inside() -> None:
    # A clip near-zero seconds: the planner still returns N samples but
    # every one must sit strictly inside the clip's tiny window.
    times = plan_sample_times(0.1, 8)
    assert 1 <= len(times) <= 8
    assert all(0 < t < 0.1 for t in times)


def test_plan_sample_times_matches_default_config_size() -> None:
    # 15s clip, 8 samples — the compile default. Must return exactly 8.
    times = plan_sample_times(15.0, 8)
    assert len(times) == 8
    # And every sample is strictly inside the clip
    assert all(0 < t < 15 for t in times)
