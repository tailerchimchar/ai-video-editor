from __future__ import annotations

import pytest
from _play_factory import make_play

from ai_video_editor.montage.plan import fit_plays, jump_cut_segments


def test_segments_keep_engage_and_outcome():
    p = make_play(engage=100.0, outcome=120.0)
    segs = jump_cut_segments(p, [105.0, 107.0], source="a.mp4")
    assert len(segs) == 1
    assert segs[0].start_s == 97.0  # engage - lead
    assert segs[0].end_s == 120.0
    assert segs[0].kill_offsets_s == [8.0, 10.0]


def test_gap_over_3s_is_cut_with_1s_after_and_1_5s_before():
    p = make_play(engage=100.0, outcome=130.0)
    segs = jump_cut_segments(p, [105.0, 115.0], source="a.mp4", gap_s=3.0)
    assert len(segs) == 2
    assert segs[0].end_s == 106.0
    assert segs[1].start_s == 113.5
    assert segs[1].end_s == 130.0
    assert segs[1].kill_offsets_s == [1.5]


def test_only_last_segment_snappable():
    p = make_play(outcome=130.0)
    segs = jump_cut_segments(p, [105.0, 115.0, 125.0], source="a.mp4")
    assert [s.is_last for s in segs] == [False, False, True]


def test_fallback_window_from_kill_times():
    p = make_play(engage=None, outcome=None, first_kill=105.0, last_kill=110.0)
    segs = jump_cut_segments(p, [105.0, 110.0], source="a.mp4", gap_s=10.0)
    assert segs[0].start_s == 105.0 - 8.0 - 3.0
    assert segs[-1].end_s == 113.0


def _plays(n: int):
    # numbers_faced descending -> rank follows list order; each ~20 s long.
    return [
        make_play(f"p{i}", numbers_faced=10 - i, engage=100.0, outcome=117.0, first_kill=105.0)
        for i in range(1, n + 1)
    ]


def _fit(plays, slot):
    return fit_plays(
        plays,
        slot_s=slot,
        kill_times={p.id: [105.0] for p in plays},
        sources={p.id: f"{p.id}.mp4" for p in plays},
        gap_s=3.0,
    )


def test_fit_drops_lowest_rank_until_slot():
    plan = _fit(_plays(4), slot=65.0)  # 4 x 20 = 80 -> drop one
    assert plan.dropped == ["p4"]
    assert not plan.over_slot
    assert len(plan.segments) == 3


def test_fit_keeps_two_plays_and_flags_over_slot():
    plan = _fit(_plays(4), slot=10.0)
    assert plan.dropped == ["p4", "p3"]
    assert plan.over_slot is True
    assert len(plan.segments) == 2


def test_order_rising_drop_falling():
    plan = _fit(_plays(5), slot=1000.0)
    assert [s.play_id for s in plan.segments] == ["p4", "p2", "p1", "p3", "p5"]


def test_single_play_not_over_slot_check():
    plan = _fit(_plays(1), slot=5.0)
    assert plan.dropped == []
    assert plan.over_slot is True
    assert pytest.approx(plan.segments[0].duration_s) == 20.0
