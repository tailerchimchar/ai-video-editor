"""Tests for `anchoring.py` — pure snap logic, no I/O."""

from __future__ import annotations

import pytest

from ai_video_editor.anchoring import (
    auto_windows_from_anchors,
    buffers_for,
    snap_window_to_anchor,
)
from ai_video_editor.candidates.overwolf_events import EventAnchor


def _anchor(seconds: float, event_type: str = "kill") -> EventAnchor:
    return EventAnchor(
        anchor_seconds=seconds,
        event_type=event_type,
        metadata={"anchor_seconds": seconds},
    )


def test_snap_pulls_window_to_nearby_kill() -> None:
    # Window midpoint at 10s; kill anchor at 11s (within 5s tolerance).
    out = snap_window_to_anchor(5.0, 15.0, [_anchor(11.0)], duration_seconds=60.0)
    assert out.snapped is True
    assert out.anchor_seconds == 11.0
    # Default kill buffers: pre=3, post=2.5 -> [8, 13.5]
    assert out.start_seconds == 8.0
    assert out.end_seconds == 13.5


def test_snap_falls_through_when_no_anchor_close() -> None:
    # Only anchor is 20s away from midpoint; keep original window.
    out = snap_window_to_anchor(5.0, 15.0, [_anchor(35.0)], duration_seconds=60.0)
    assert out.snapped is False
    assert out.anchor_seconds is None
    assert (out.start_seconds, out.end_seconds) == (5.0, 15.0)


def test_snap_empty_anchors_returns_original() -> None:
    out = snap_window_to_anchor(0.0, 10.0, [], duration_seconds=60.0)
    assert out.snapped is False
    assert (out.start_seconds, out.end_seconds) == (0.0, 10.0)


def test_snap_uses_per_type_buffers_for_ace() -> None:
    out = snap_window_to_anchor(80.0, 100.0, [_anchor(90.0, "ace")], duration_seconds=200.0)
    # ace buffers: pre=6, post=4 -> [84, 94]
    assert out.event_type == "ace"
    assert out.start_seconds == 84.0
    assert out.end_seconds == 94.0


def test_snap_clamps_to_duration_bounds() -> None:
    # Anchor near the end -> post buffer would exceed duration.
    out = snap_window_to_anchor(55.0, 62.0, [_anchor(59.5)], duration_seconds=60.0)
    assert out.end_seconds == 60.0
    # And near the start -> pre buffer would go negative.
    out = snap_window_to_anchor(0.0, 5.0, [_anchor(1.0)], duration_seconds=60.0)
    assert out.start_seconds == 0.0


def test_snap_picks_closest_when_multiple_anchors_within_range() -> None:
    anchors = [_anchor(6.0), _anchor(11.0), _anchor(18.0)]
    # midpoint = 10; closest is 11
    out = snap_window_to_anchor(5.0, 15.0, anchors, duration_seconds=60.0)
    assert out.anchor_seconds == 11.0


def test_snap_reversed_window_raises() -> None:
    with pytest.raises(ValueError):
        snap_window_to_anchor(15.0, 5.0, [_anchor(10.0)], duration_seconds=60.0)


def test_buffers_for_falls_back_to_default_on_unknown_type() -> None:
    pre, post = buffers_for("something_random")
    assert pre == 3.0 and post == 2.5


def test_auto_windows_single_anchor() -> None:
    windows = auto_windows_from_anchors([_anchor(10.0)], duration_seconds=60.0)
    assert len(windows) == 1
    assert windows[0].start_seconds == 7.0
    assert windows[0].end_seconds == 12.5


def test_auto_windows_merges_overlapping_kills() -> None:
    # Two kills 4s apart -> windows overlap -> merge to one
    windows = auto_windows_from_anchors([_anchor(10.0), _anchor(14.0)], duration_seconds=60.0)
    assert len(windows) == 1
    assert windows[0].start_seconds == 7.0  # from first
    assert windows[0].end_seconds == 16.5  # from second


def test_auto_windows_keeps_separate_when_far_apart() -> None:
    windows = auto_windows_from_anchors([_anchor(10.0), _anchor(40.0)], duration_seconds=60.0)
    assert len(windows) == 2


def test_auto_windows_merges_kill_with_cluster_row() -> None:
    # Overwolf emits both individual kills AND the cluster row.
    # They should merge into ONE window using the cluster's buffers.
    anchors = [_anchor(50.0), _anchor(52.0), _anchor(51.0, "multi_kill_3")]
    windows = auto_windows_from_anchors(anchors, duration_seconds=100.0)
    assert len(windows) == 1
    # First-seen event type wins in merge (order after sort by seconds).
    # Sort by seconds -> 50, 51, 52. First is kill @ 50 -> pre=3, post=2.5
    # But merged end will extend to cover 52's post window.
    assert windows[0].start_seconds == 47.0
    assert windows[0].end_seconds >= 54.5


def test_auto_windows_empty() -> None:
    assert auto_windows_from_anchors([], duration_seconds=60.0) == []


def test_auto_windows_handles_unsorted_input() -> None:
    windows = auto_windows_from_anchors(
        [_anchor(40.0), _anchor(10.0), _anchor(25.0)], duration_seconds=60.0
    )
    assert len(windows) == 3
    # Sorted by anchor seconds
    assert windows[0].anchor_seconds == 10.0
    assert windows[1].anchor_seconds == 25.0
    assert windows[2].anchor_seconds == 40.0
