"""rank (pure): score x pop, drop none, one letterbox, no overlaps, inside
the chapter, at most 6; default params and spans."""

from __future__ import annotations

from ai_video_editor.story.suggest.models import Candidate, EffectChoice
from ai_video_editor.story.suggest.params import effect_span
from ai_video_editor.story.suggest.rank import MAX_ITEMS, rank, reason
from ai_video_editor.story.suggest.verdict import Verdict


def _cand(cid, start, end, chapter=0, anchor=None, **facts) -> Candidate:
    base = {
        "minute": int(start // 60),
        "kills": 1,
        "deaths": 0,
        "numbers": None,
        "outnumbered_win": False,
        "multikill": 1,
        "objective": None,
        "is_last_fight": False,
        "enemy_champions": [],
    }
    return Candidate(
        id=cid,
        chapter_index=chapter,
        start_s=start,
        end_s=end,
        facts={**base, **facts},
        anchor_s=anchor,
    )


def _v(effect, score=3, pop_p=None, backend="jev") -> Verdict:
    return Verdict(
        score=score,
        score_p=0.8 if backend == "jev" else None,
        pop_p=pop_p,
        effect=effect,
        effect_p=0.7 if backend == "jev" else None,
        backend=backend,
    )


WIDE = {i: (0.0, 10_000.0) for i in range(20)}


def test_orders_by_score_times_pop_and_drops_none():
    scored = [
        (_cand("a", 100, 110), _v(EffectChoice.zoom, score=3, pop_p=0.9)),  # 2.7
        (_cand("b", 200, 210), _v(EffectChoice.zoom, score=5, pop_p=None)),  # 2.5
        (_cand("c", 300, 310), _v(EffectChoice.zoom, score=4, pop_p=0.8)),  # 3.2
        (_cand("d", 400, 410), _v(EffectChoice.none, score=5, pop_p=1.0)),
    ]
    items = rank(scored, WIDE)
    assert [i.id.split("-")[1] for i in items] == ["c", "a", "b"]
    assert items[2].pop_p is None and items[0].pop_p == 0.8


def test_at_most_one_letterbox_focus():
    scored = [
        (_cand("a", 100, 110), _v(EffectChoice.letterbox_focus, score=5, pop_p=0.9)),
        (_cand("b", 200, 210), _v(EffectChoice.letterbox_focus, score=4, pop_p=0.9)),
    ]
    items = rank(scored, WIDE)
    assert [(i.kind, i.id.split("-")[1]) for i in items] == [("letterbox_focus", "a")]


def test_no_overlaps_keeps_the_better_one():
    scored = [
        (_cand("a", 100, 120), _v(EffectChoice.zoom, score=2)),
        (_cand("b", 110, 130), _v(EffectChoice.zoom, score=5)),
        (_cand("c", 130, 140), _v(EffectChoice.zoom, score=1)),  # touching is fine
    ]
    assert [i.id.split("-")[1] for i in rank(scored, WIDE)] == ["b", "c"]


def test_cap_at_six():
    scored = [(_cand(f"x{i}", i * 100, i * 100 + 5), _v(EffectChoice.zoom)) for i in range(9)]
    assert MAX_ITEMS == 6
    assert len(rank(scored, WIDE)) == 6


def test_panel_picking_none_everywhere_is_topped_up_to_three():
    # Real Jhin run 2026-11-25: Jev said "none" for all 13 fights.
    none = EffectChoice.none
    scored = [
        (_cand("k1", 100, 110, anchor=105), _v(none, score=2, pop_p=0.3)),
        (_cand("quiet", 200, 210, kills=0), _v(none, score=2, pop_p=0.9)),
        (_cand("k2", 300, 310, anchor=305), _v(none, score=2, pop_p=0.2)),
        (_cand("last", 400, 420, anchor=410, is_last_fight=True), _v(none, score=3, pop_p=0.2)),
    ]
    out = rank(scored, WIDE)
    assert [(i.id, i.kind) for i in out] == [
        ("s-last-letterbox_focus", "letterbox_focus"),
        ("s-k1-slowmo", "slowmo"),
        ("s-k2-slowmo", "slowmo"),
    ]
    assert all(i.reason.startswith("top-up (") for i in out)


def test_top_up_never_replaces_panel_picks_and_stops_at_three():
    scored = [
        (_cand("a", 100, 110, anchor=105), _v(EffectChoice.zoom, score=4, pop_p=0.9)),
        (_cand("b", 200, 210, anchor=205), _v(EffectChoice.none, score=2, pop_p=0.5)),
        (_cand("c", 300, 310, anchor=305), _v(EffectChoice.none, score=2, pop_p=0.4)),
        (_cand("d", 400, 410, anchor=405), _v(EffectChoice.none, score=2, pop_p=0.3)),
    ]
    out = rank(scored, WIDE)
    assert [i.id for i in out] == ["s-a-zoom", "s-b-slowmo", "s-c-slowmo"]
    assert not out[0].reason.startswith("top-up")


def test_fewer_than_three_is_fine_when_nothing_can_be_topped_up():
    assert len(rank([(_cand("a", 1, 5), _v(EffectChoice.zoom))], WIDE)) == 1
    assert rank([], WIDE) == []


def test_clamped_inside_chapter_and_dropped_when_outside():
    scored = [
        (_cand("a", 100, 130, chapter=0), _v(EffectChoice.zoom)),
        (_cand("b", 500, 520, chapter=1), _v(EffectChoice.zoom)),
    ]
    items = rank(scored, {0: (105.0, 125.0), 1: (0.0, 50.0)})
    assert [(i.start_s, i.end_s, i.chapter_index) for i in items] == [(105.0, 125.0, 0)]


def test_default_params_and_slowmo_span():
    c = _cand("a", 100, 120, anchor=110)
    assert effect_span(EffectChoice.letterbox_focus, c) == (100, 120, {"bar": 0.12})
    assert effect_span(EffectChoice.zoom, c) == (100, 120, {"factor": 1.3, "roi": "center"})
    start, end, params = effect_span(EffectChoice.slowmo, c)
    assert params == {"factor": 0.5}
    assert start <= 110 <= end and end - start <= 2.5
    # near the candidate's end the window shifts but never leaves the candidate
    late = _cand("b", 100, 111, anchor=110.5)
    s2, e2, _ = effect_span(EffectChoice.slowmo, late)
    assert s2 >= 100 and e2 <= 111 and e2 - s2 <= 2.5


def test_reason_reads_the_answers():
    c = _cand("a", 1000, 1010, numbers="4v5", outnumbered_win=True)
    v = Verdict(
        score=5, score_p=0.82, pop_p=0.91, effect=EffectChoice.slowmo, effect_p=0.6, backend="jev"
    )
    assert reason(c, v).startswith("4v5 won · play 5/5 (p .82) · pop (p .91)")
    rules = Verdict(
        score=4,
        score_p=None,
        pop_p=None,
        effect=EffectChoice.slowmo,
        effect_p=None,
        backend="rules",
    )
    text = reason(c, rules)
    assert "play 4/5 (default)" in text and "pop (no opinion)" in text


def test_item_ids_are_stable():
    scored = [(_cand("a", 100, 110), _v(EffectChoice.zoom))]
    assert rank(scored, WIDE)[0].id == rank(scored, WIDE)[0].id == "s-a-zoom"
