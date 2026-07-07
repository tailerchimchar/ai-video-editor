"""Beat-sync tests. Pure functions — no ffmpeg, no I/O."""

from __future__ import annotations

import pytest

from ai_video_editor.music.sync import (
    ClipWindow,
    _nearest_beat,
    snap_clips_to_beats,
)


def test_nearest_beat_at_exact_beat() -> None:
    beats = (0.0, 0.5, 1.0, 1.5)
    assert _nearest_beat(1.0, beats) == 1.0


def test_nearest_beat_between_two() -> None:
    beats = (0.0, 1.0)
    # 0.4 is closer to 0.0
    assert _nearest_beat(0.4, beats) == 0.0
    # 0.6 is closer to 1.0
    assert _nearest_beat(0.6, beats) == 1.0


def test_nearest_beat_below_range_clamps_to_first() -> None:
    beats = (5.0, 6.0)
    assert _nearest_beat(1.0, beats) == 5.0


def test_nearest_beat_above_range_clamps_to_last() -> None:
    beats = (5.0, 6.0)
    assert _nearest_beat(100.0, beats) == 6.0


def test_nearest_beat_empty_returns_input() -> None:
    assert _nearest_beat(0.5, ()) == 0.5


def test_snap_unknown_strategy_raises() -> None:
    with pytest.raises(ValueError, match="unknown sync strategy"):
        snap_clips_to_beats([], (0.0, 0.5), strategy="ballet")


def test_kill_on_beat_shifts_first_clip_to_land_anchor_on_beat() -> None:
    """First clip has anchor at t=1.0 (from clip start). Beats are at
    0.0, 0.5, 1.0, 1.5. Anchor should land exactly on 1.0 → no shift."""
    clip = ClipWindow(
        file="a.mp4",
        start_seconds=100.0,
        end_seconds=110.0,
        anchor_offset_seconds=1.0,
    )
    snap = snap_clips_to_beats([clip], (0.0, 0.5, 1.0, 1.5))
    assert len(snap.clips) == 1
    assert snap.clips[0].beat_time_seconds == 1.0
    assert abs(snap.clips[0].shift_seconds) < 1e-6


def test_kill_on_beat_shifts_to_nearest_beat_within_tolerance() -> None:
    """Anchor at 0.9 → nearest beat is 1.0 → shift +0.1 (pre-roll)."""
    clip = ClipWindow(
        file="a.mp4",
        start_seconds=100.0,
        end_seconds=110.0,
        anchor_offset_seconds=0.9,
    )
    snap = snap_clips_to_beats(
        [clip], (0.0, 0.5, 1.0, 1.5), tolerance_seconds=0.5
    )
    assert snap.clips[0].beat_time_seconds == 1.0
    assert abs(snap.clips[0].shift_seconds - 0.1) < 1e-6
    # Shift is applied by moving start EARLIER (pre-roll before anchor)
    assert abs(snap.clips[0].new_start_seconds - 99.9) < 1e-6


def test_kill_on_beat_clamps_at_tolerance() -> None:
    """Anchor at 2.0, beats only at 0/5/10 → nearest beat 0 or 5, both
    >tolerance away. Shift should clamp."""
    clip = ClipWindow(
        file="a.mp4",
        start_seconds=100.0,
        end_seconds=110.0,
        anchor_offset_seconds=2.0,
    )
    snap = snap_clips_to_beats(
        [clip], (0.0, 5.0, 10.0), tolerance_seconds=0.5
    )
    # Nearest beat to 2.0 is 0.0 (distance 2.0); tolerance clamps shift to 0.5
    assert abs(snap.clips[0].shift_seconds) == pytest.approx(0.5, abs=1e-6)


def test_kill_on_beat_second_clip_uses_montage_time() -> None:
    """Two clips: first is 10s, its anchor at t=1.0 lands on beat=1.0.
    Second clip starts at montage_t = 10 - 0.0 = 10.0 (no shift on first).
    Second's anchor at t=1.5 in clip-relative = 10 + 1.5 = 11.5 in
    montage time. Nearest beat at 11.5."""
    clips = [
        ClipWindow(
            file="a.mp4",
            start_seconds=0.0,
            end_seconds=10.0,
            anchor_offset_seconds=1.0,
        ),
        ClipWindow(
            file="b.mp4",
            start_seconds=0.0,
            end_seconds=10.0,
            anchor_offset_seconds=1.5,
        ),
    ]
    # Beats: 1.0 (first clip lands here) and 11.5 (second lands here exactly)
    beats = tuple(x * 0.5 for x in range(30))
    snap = snap_clips_to_beats(clips, beats)
    assert snap.clips[0].beat_time_seconds == 1.0
    # Second clip's anchor at t=11.5 aligns with the 11.5 beat
    assert snap.clips[1].beat_time_seconds == pytest.approx(11.5, abs=0.1)


def test_snap_never_produces_negative_start() -> None:
    """A clip near t=0 that needs a large positive shift (pre-roll)
    must clamp to start=0."""
    clip = ClipWindow(
        file="a.mp4",
        start_seconds=0.05,
        end_seconds=10.0,
        anchor_offset_seconds=0.1,
    )
    # A beat at 0.5 → wants to shift start EARLIER by 0.4 → negative
    snap = snap_clips_to_beats([clip], (0.5,), tolerance_seconds=2.0)
    assert snap.clips[0].new_start_seconds >= 0.0


def test_snap_is_deterministic() -> None:
    """Same input → identical output every time."""
    clips = [
        ClipWindow(
            file="a.mp4",
            start_seconds=100.0,
            end_seconds=110.0,
            anchor_offset_seconds=1.0,
        )
    ]
    beats = (0.0, 0.5, 1.0, 1.5, 2.0)
    r1 = snap_clips_to_beats(clips, beats)
    r2 = snap_clips_to_beats(clips, beats)
    assert r1 == r2
