"""Tests for multi-kill clustering in overwolf_events.

Pure -- exercises `_cluster_multikills` directly with synthetic kill
rows. The wider end-to-end coverage stays in `test_overwolf_events.py`.
"""

from __future__ import annotations

from ai_video_editor.candidates.overwolf_events import _cluster_multikills


def _kill(anchor: float, headshot: bool = False) -> dict:
    return {
        "start_seconds": max(0.0, anchor - 5.0),
        "end_seconds": anchor + 5.0,
        "event_type": "kill",
        "confidence": 1.0,
        "metadata": {
            "anchor_seconds": anchor,
            "headshot": headshot,
        },
    }


def test_two_kills_within_window_produce_multi_kill_2() -> None:
    rows = _cluster_multikills(
        [_kill(10.0), _kill(15.0)], duration_seconds=600.0
    )
    assert len(rows) == 1
    assert rows[0]["event_type"] == "multi_kill_2"
    assert rows[0]["metadata"]["cluster_size"] == 2


def test_three_kills_within_window_produce_multi_kill_3() -> None:
    rows = _cluster_multikills(
        [_kill(10.0), _kill(15.0), _kill(20.0)], duration_seconds=600.0
    )
    assert len(rows) == 1
    assert rows[0]["event_type"] == "multi_kill_3"


def test_four_kills_produce_multi_kill_4() -> None:
    rows = _cluster_multikills(
        [_kill(10.0), _kill(15.0), _kill(20.0), _kill(25.0)],
        duration_seconds=600.0,
    )
    assert rows[0]["event_type"] == "multi_kill_4"
    assert rows[0]["metadata"]["cluster_size"] == 4


def test_five_kills_produce_ace() -> None:
    rows = _cluster_multikills(
        [_kill(10.0), _kill(15.0), _kill(20.0), _kill(25.0), _kill(28.0)],
        duration_seconds=600.0,
    )
    assert rows[0]["event_type"] == "ace"
    assert rows[0]["metadata"]["cluster_size"] == 5


def test_kills_beyond_window_split_into_separate_clusters() -> None:
    """Kills at 10 + 15 -> cluster A; 60 + 65 -> cluster B."""
    rows = _cluster_multikills(
        [_kill(10.0), _kill(15.0), _kill(60.0), _kill(65.0)],
        duration_seconds=600.0,
    )
    assert len(rows) == 2
    assert rows[0]["metadata"]["cluster_size"] == 2
    assert rows[1]["metadata"]["cluster_size"] == 2
    assert rows[0]["metadata"]["first_kill_seconds"] == 10.0
    assert rows[1]["metadata"]["first_kill_seconds"] == 60.0


def test_single_kill_produces_no_cluster() -> None:
    """A lone kill can't be a multi-kill."""
    assert _cluster_multikills([_kill(10.0)], duration_seconds=600.0) == []


def test_extending_cluster_uses_last_anchor_not_first() -> None:
    """Kills at 0, 25, 50 with a 30s window: 0->25 within window (25s),
    25->50 within window (25s from PREVIOUS kill). All three cluster."""
    rows = _cluster_multikills(
        [_kill(0.0), _kill(25.0), _kill(50.0)],
        duration_seconds=600.0,
    )
    assert len(rows) == 1
    assert rows[0]["metadata"]["cluster_size"] == 3


def test_start_end_wrap_the_cluster_with_lead_and_trail() -> None:
    """Cluster window extends beyond the first/last kill anchors."""
    rows = _cluster_multikills(
        [_kill(20.0), _kill(28.0)], duration_seconds=600.0,
    )
    # _MULTIKILL_LEAD_SECONDS = 6, _MULTIKILL_TRAIL_SECONDS = 4
    assert rows[0]["start_seconds"] == 14.0  # 20 - 6
    assert rows[0]["end_seconds"] == 32.0    # 28 + 4


def test_cluster_start_never_goes_negative() -> None:
    rows = _cluster_multikills(
        [_kill(2.0), _kill(4.0)], duration_seconds=600.0,
    )
    assert rows[0]["start_seconds"] == 0.0


def test_cluster_end_clamped_to_video_duration() -> None:
    """Last kill at t=599, trail=4s would push to 603 -- clamp to 600."""
    rows = _cluster_multikills(
        [_kill(595.0), _kill(599.0)], duration_seconds=600.0,
    )
    assert rows[0]["end_seconds"] == 600.0


def test_headshot_count_reported_in_metadata() -> None:
    rows = _cluster_multikills(
        [
            _kill(10.0, headshot=True),
            _kill(14.0),
            _kill(18.0, headshot=True),
        ],
        duration_seconds=600.0,
    )
    assert rows[0]["metadata"]["headshot_count"] == 2
    assert "2 headshots" in rows[0]["metadata"]["rationale"]


def test_cluster_confidence_is_ground_truth() -> None:
    rows = _cluster_multikills(
        [_kill(10.0), _kill(15.0)], duration_seconds=600.0,
    )
    assert rows[0]["confidence"] == 1.0


def test_empty_input_returns_empty() -> None:
    assert _cluster_multikills([], duration_seconds=600.0) == []
