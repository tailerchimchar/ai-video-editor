"""plan_story_from_plays: chapters 1:1 from owner-involved plays."""

from __future__ import annotations

import pytest
from _play_factory import make_play

from ai_video_editor.league.play_models import GameModel
from ai_video_editor.league.riot_timeline import RiotTimeline, TimelineEvent
from ai_video_editor.story.planner import (
    ENDING_TAIL_S,
    ENDING_TITLE,
    EVENT_POST_S,
    Chapter,
    _with_ending,
    plan_story_from_plays,
)


# game_end_s defaults to None: plan_story_from_plays ends every plan on the
# nexus when it is known (_with_ending), which these tests are not about.
def _tl(kills: list[float] | None = None, end: float | None = None) -> RiotTimeline:
    events = [TimelineEvent("kill", t, t, [], {}) for t in kills or []]
    return RiotTimeline("m", "high", 0.0, end, events, [])


def _game(*plays) -> GameModel:
    return GameModel(plays=list(plays))


def _plan(game, tl=None, **kw):
    return plan_story_from_plays(game, tl or _tl(), mic_stream=None, **kw)


def test_one_chapter_per_play_no_merge():
    g = _game(make_play("a", approach=100, outcome=110), make_play("b", approach=140, outcome=150))
    assert [c.play_id for c in _plan(g).chapters] == ["a", "b"]


def test_no_45s_cap():
    plan = _plan(_game(make_play("a", approach=100, outcome=158)))
    assert plan.chapters[0].end_s - plan.chapters[0].start_s == 58


def test_drop_whole_plays_by_rank_until_fit():
    g = _game(
        make_play("a", approach=100, outcome=140, rank=1),
        make_play("b", approach=200, outcome=240, rank=3),
        make_play("c", approach=300, outcome=340, rank=2),
    )
    plan = _plan(g, max_s=85)
    assert [c.play_id for c in plan.chapters] == ["a", "c"]
    assert all(c.end_s - c.start_s == 40 for c in plan.chapters)
    assert not plan.over_budget


def test_close_never_dropped_and_over_budget():
    g = _game(
        make_play("a", approach=100, outcome=140, rank=1),
        make_play("z", type="closeout", approach=300, outcome=360, rank=2),
    )
    plan = _plan(g, max_s=30)
    assert [c.play_id for c in plan.chapters] == ["z"]
    assert plan.over_budget


def test_absent_plays_excluded():
    g = _game(make_play("a"), make_play("b", involved=False))
    assert [c.play_id for c in _plan(g).chapters] == ["a"]


def test_kill_times_inside_chapter():
    g = _game(make_play("a", approach=100, outcome=120))
    plan = _plan(g, _tl([105.0, 300.0]))
    assert plan.chapters[0].kill_times_s == [105.0]


def test_fallback_window_when_vlm_pending():
    g = _game(make_play("a", approach=None, outcome=None, first_kill=200, last_kill=210))
    c = _plan(g).chapters[0]
    assert (c.start_s, c.end_s) == (192.0, 213.0)


def test_clamped_to_game_end():
    g = _game(make_play("a", approach=1990, outcome=2100))
    # Clamped to the game end, then (owner 2026-11-26) stretched over the nexus.
    assert _plan(g, _tl(end=2000.0)).chapters[0].end_s == 2000.0 + ENDING_TAIL_S


def test_close_and_lane_kinds():
    g = _game(
        make_play("e", phase="early", approach=10, outcome=30),
        make_play("z", type="closeout", approach=300, outcome=330),
        make_play("f", approach=100, outcome=120),
    )
    assert {c.play_id: c.kind for c in _plan(g).chapters} == {
        "e": "lane",
        "f": "fight",
        "z": "close",
    }


def test_titles_are_play_text_and_cards_false():
    plan = _plan(_game(make_play("a", text="Jhin 1v3")))
    assert plan.chapters[0].title == "Jhin 1v3"
    assert plan.cards is False


def test_banner_offset_correction_shifts_kill_times():
    g = _game(make_play("a", approach=100, outcome=120, offset_correction=1.5))
    plan = _plan(g, _tl([105.0, 119.0]))
    # 119 + 1.5 lands 0.5 s past the outcome: pulled in, with EVENT_POST_S after it.
    assert plan.chapters[0].kill_times_s == [106.5, 120.5]
    assert plan.chapters[0].end_s == 120.5 + EVENT_POST_S


def test_chapters_15s_apart_never_repeat_footage():
    g = _game(
        make_play("a", approach=100, outcome=130, first_kill=110, last_kill=125),
        make_play("b", approach=115, outcome=150, first_kill=125, last_kill=145),
    )
    plan = _plan(g, _tl([110.0, 125.0, 145.0]))
    a, b = plan.chapters
    assert (a.start_s, a.end_s) == (100, 130)
    assert (b.start_s, b.end_s) == (130, 150)
    assert b.kill_times_s == [145.0]
    assert plan.total_s == 50


def test_chapter_inside_previous_one_is_dropped():
    g = _game(
        make_play("a", approach=100, outcome=160),
        make_play("b", approach=115, outcome=150),
    )
    assert [c.play_id for c in _plan(g).chapters] == ["a"]


# --- early-game chapters (AVE-57) ---

from ai_video_editor.config import settings  # noqa: E402
from ai_video_editor.league.early_game import EarlySegment  # noqa: E402


def _seg(kind, a, b):
    return EarlySegment(kind=kind, start_s=a, end_s=b, reason="r")


def test_early_chapters_first_with_templated_titles():
    g = _game(make_play("a", approach=300, outcome=320))
    plan = _plan(g, early=[_seg("first_back", 200, 220), _seg("opening", 30, 100)])
    assert [(c.kind, c.start_s, c.end_s) for c in plan.chapters[:2]] == [
        ("early", 30, 100),
        ("early", 200, 220),
    ]
    assert plan.chapters[0].title == "The opening"
    assert plan.chapters[1].title == "First back"
    assert plan.chapters[2].play_id == "a"


def test_late_back_chapters_skip_the_lane_and_title_heavy_trades():
    # Owner 2026-11-26: a late first back keeps the opening, heavy trades and
    # the recall lead-up as separate chapters; the quiet lane between is cut.
    early = [_seg("opening", 30, 130), _seg("heavy_trade", 200, 218), _seg("first_back", 362, 408)]
    plan = _plan(_game(), early=early)
    assert [(c.title, c.start_s, c.end_s) for c in plan.chapters if c.kind == "early"] == [
        ("The opening", 30, 130),
        ("Heavy trade", 200, 218),
        ("First back", 362, 408),
    ]


def test_early_gaps_under_4s_merge():
    plan = _plan(_game(), early=[_seg("opening", 30, 100), _seg("level3_trade", 102, 130)])
    assert [(c.start_s, c.end_s) for c in plan.chapters] == [(30, 130)]


def test_early_never_dropped_under_budget_pressure():
    g = _game(
        make_play("a", approach=300, outcome=340, rank=1),
        make_play("b", approach=400, outcome=440, rank=2),
    )
    plan = _plan(g, max_s=60, early=[_seg("opening", 30, 100)])
    # early rides on top of the plays budget: it never costs a play
    assert [c.kind for c in plan.chapters] == ["early", "fight"]
    assert [c.play_id for c in plan.chapters][1] == "a"
    assert not plan.over_budget


def test_early_does_not_count_against_budget():
    plan = _plan(_game(make_play("a")), max_s=50, early=[_seg("opening", 30, 100)])
    assert plan.chapters[0].kind == "early"
    assert plan.total_s > 50 and not plan.over_budget


def test_no_early_is_old_behaviour():
    g = _game(make_play("a", approach=300, outcome=320))
    assert _plan(g, early=None).chapters == _plan(g).chapters


def test_late_lead_in_only_after_20_minutes_game_time(monkeypatch):
    monkeypatch.setattr(settings, "story_late_lead_in_s", 5.0)
    g = _game(
        make_play("early", approach=1100, outcome=1110),
        make_play("late", approach=1500, outcome=1510),
    )
    starts = {c.play_id: c.start_s for c in _plan(g).chapters}
    assert starts == {"early": 1100, "late": 1495}


def test_late_lead_in_uses_offset_for_game_time(monkeypatch):
    monkeypatch.setattr(settings, "story_late_lead_in_s", 5.0)
    tl = RiotTimeline("m", "high", 100.0, 3000.0, [], [])
    # recorded with offset 100 too: game 1150 s, no lead-in
    g = _game(make_play("a", approach=1250, outcome=1260, rec_minus_game=100.0))
    assert _plan(g, tl).chapters[0].start_s == 1250


def test_late_lead_in_never_before_previous_chapter_end(monkeypatch):
    monkeypatch.setattr(settings, "story_late_lead_in_s", 5.0)
    g = _game(
        make_play("a", approach=1300, outcome=1320),
        make_play("b", approach=1322, outcome=1340),
    )
    assert [c.start_s for c in _plan(g).chapters] == [1295, 1320]


def test_kill_times_kept_in_early_chapter():
    plan = _plan(_game(), _tl([50.0, 500.0]), early=[_seg("opening", 30, 100)])
    assert plan.chapters[0].kill_times_s == [50.0]


# --- AVE-62: rebase on the current offset, HUD wins, death times, outnumbered win ---


def _ev(kind, t):
    return TimelineEvent(kind, t, t, [], {})


def _tl_events(events, offset=0.0, source=None, end=None):
    tl = RiotTimeline("m", "high", offset, end, events, [])
    tl.offset_source = source
    return tl


def test_rebase_moves_window_by_offset_change():
    g = _game(make_play("a", approach=100, outcome=120))  # written with offset 0
    c = _plan(g, _tl_events([_ev("kill", 115.0)], offset=10.0)).chapters[0]
    assert (c.start_s, c.end_s) == (110, 130)
    assert c.kill_times_s == [115.0]


def test_rebase_moves_fallback_window():
    g = _game(make_play("a", approach=None, outcome=None, first_kill=200, last_kill=210))
    c = _plan(g, _tl_events([], offset=-5.0)).chapters[0]
    assert (c.start_s, c.end_s) == (187.0, 208.0)


def test_rebase_zero_when_offset_unchanged():
    g = _game(make_play("a", approach=100, outcome=120, rec_minus_game=7.0))
    c = _plan(g, _tl_events([], offset=7.0)).chapters[0]
    assert (c.start_s, c.end_s) == (100, 120)


def test_hud_offset_ignores_offset_correction():
    g = _game(make_play("a", approach=100, outcome=120, offset_correction=1.5))
    tl = _tl_events([_ev("kill", 105.0), _ev("kill", 119.0)], source="hud")
    assert _plan(g, tl).chapters[0].kill_times_s == [105.0, 119.0]


def test_non_hud_offset_still_applies_correction():
    g = _game(make_play("a", approach=100, outcome=120, offset_correction=1.5))
    tl = _tl_events([_ev("kill", 105.0), _ev("kill", 119.0)], source="audio")
    assert _plan(g, tl).chapters[0].kill_times_s == [106.5, 120.5]


def test_death_times_filled_like_kill_times():
    g = _game(make_play("a", approach=100, outcome=120, offset_correction=1.5))
    tl = _tl_events([_ev("death", 110.0), _ev("death", 119.0), _ev("death", 300.0)])
    assert _plan(g, tl).chapters[0].death_times_s == [111.5, 120.5]


def test_death_times_hud_ignores_correction():
    g = _game(make_play("a", approach=100, outcome=120, offset_correction=1.5))
    tl = _tl_events([_ev("death", 119.0)], source="hud")
    assert _plan(g, tl).chapters[0].death_times_s == [119.0]


def test_early_chapter_gets_death_times():
    plan = _plan(
        _game(),
        _tl_events([_ev("death", 50.0), _ev("death", 500.0)]),
        early=[_seg("opening", 30, 100)],
    )
    assert plan.chapters[0].death_times_s == [50.0]


def test_no_overlap_trims_death_times():
    g = _game(
        make_play("a", approach=100, outcome=130),
        make_play("b", approach=115, outcome=150),
    )
    tl = _tl_events([_ev("death", 120.0), _ev("death", 140.0)])
    a, b = _plan(g, tl).chapters
    assert a.death_times_s == [120.0]
    assert b.start_s == 130
    assert b.death_times_s == [140.0]


# Zeri-shaped: recording = game - 17 s. Urgot (ally) dies game 33:49.3 (rec 2012.3),
# the next kills are game 34:24-34:44 (rec 2047-2067), closeout approach rec 2042.
@pytest.fixture(autouse=True)
def _no_late_lead(monkeypatch):
    monkeypatch.setattr(settings, "story_late_lead_in_s", 0.0)


def _zeri(outcome="won", approach=2042.0, end=2068.0, numbers="4v5", **ev_kw):
    play = make_play(
        "p43",
        type="closeout",
        approach=approach,
        engage=2046.0,
        outcome=end,
        first_kill=2064.0,
        last_kill=2084.0,
        rec_minus_game=-17.0,
        outcome_result=outcome,
        numbers=numbers,
    )
    events = [_ev("ally_death", 2012.3), _ev("kill", 2047.0), _ev("kill", 2067.0)]
    events += ev_kw.get("extra", [])
    return _game(play), _tl_events(events, offset=-17.0, source="hud", end=2100.0)


def test_outnumbered_win_starts_at_ally_death_minus_lead():
    g, tl = _zeri()
    c = _plan(g, tl).chapters[0]
    assert c.kind == "close"
    assert c.start_s == 2009.3
    assert c.kill_times_s == [2047.0, 2067.0]


def test_outnumbered_enemy_died_between_unchanged():
    g, tl = _zeri(extra=[_ev("enemy_death", 2030.0)])
    assert _plan(g, tl).chapters[0].start_s == 2042.0


def test_outnumbered_owner_assist_between_unchanged():
    g, tl = _zeri(extra=[_ev("assist", 2030.0)])
    assert _plan(g, tl).chapters[0].start_s == 2042.0


def test_outnumbered_ally_died_90s_before_unchanged():
    g, tl = _zeri(approach=2092.0, end=2110.0)  # death is 79.7 s before approach
    tl.game_end_s = 3000.0
    assert _plan(g, tl).chapters[0].start_s == 2092.0


def test_outnumbered_lost_fight_unchanged():
    g, tl = _zeri(outcome="lost")
    assert _plan(g, tl).chapters[0].start_s == 2042.0


def test_outnumbered_picks_earliest_ally_death():
    g, tl = _zeri()
    tl.events.insert(0, _ev("ally_death", 1990.0))  # 52 s before approach
    tl.events.append(_ev("ally_death", 2020.0))
    assert _plan(g, tl).chapters[0].start_s == 1987.0


def test_outnumbered_applies_to_fight_chapter_and_clamps_at_zero():
    play = make_play("f", approach=20, outcome=60, first_kill=40, last_kill=55, numbers="4v5")
    tl = _tl_events([_ev("ally_death", 1.0), _ev("kill", 40.0)])
    assert _plan(_game(play), tl).chapters[0].start_s == 0.0


def test_outnumbered_not_for_lane_chapter():
    play = make_play("e", phase="early", approach=100, outcome=130)
    tl = _tl_events([_ev("ally_death", 80.0)])
    assert _plan(_game(play), tl).chapters[0].start_s == 100


def test_outnumbered_start_after_previous_chapter():
    prev = make_play("a", approach=1990, outcome=2030, rec_minus_game=-17.0)
    g, tl = _zeri()
    tl.game_end_s = None  # not about the ending chapter
    plan = _plan(_game(prev, g.plays[0]), tl)
    assert [c.play_id for c in plan.chapters] == ["a", "p43"]
    assert plan.chapters[1].start_s == 2030


def test_outnumbered_needs_fewer_allies_than_enemies():
    # A won 2v1 is not a comeback: an earlier ally death elsewhere doesn't pull it back.
    g, tl = _zeri(numbers="2v1")
    assert _plan(g, tl).chapters[0].start_s == 2042.0


def test_outnumbered_even_numbers_unchanged():
    g, tl = _zeri(numbers="4v4")
    assert _plan(g, tl).chapters[0].start_s == 2042.0


def test_kill_on_window_end_keeps_post_buffer():
    play = make_play("k", approach=100, outcome=120, first_kill=110, last_kill=120)
    tl = _tl_events([_ev("kill", 120.0)])
    c = _plan(_game(play), tl).chapters[0]
    assert c.kill_times_s == [120.0]
    assert c.end_s == 120.0 + EVENT_POST_S


def test_kill_just_past_window_end_is_pulled_in():
    play = make_play("k", approach=100, outcome=120, first_kill=110, last_kill=120)
    tl = _tl_events([_ev("death", 121.5)])
    c = _plan(_game(play), tl).chapters[0]
    assert c.death_times_s == [121.5]
    assert c.end_s == 121.5 + EVENT_POST_S


def test_post_buffer_never_past_game_end():
    play = make_play("k", type="closeout", approach=100, outcome=120, first_kill=110, last_kill=120)
    tl = _tl_events([_ev("kill", 120.0)], end=120.5)
    c = _plan(_game(play), tl).chapters[0]
    # The post buffer is clamped to the game end; the nexus tail (owner
    # 2026-11-26) then stretches the last chapter ENDING_TAIL_S past it.
    assert c.end_s == 120.5 + ENDING_TAIL_S


# --- _with_ending ---


def _ch(a, b, title="x", kind="fight"):
    return Chapter(title=title, start_s=a, end_s=b, kind=kind)


def _etl(end):
    return _tl(end=end)


def test_ending_stretches_last_chapter_within_20s_of_the_end():
    out = _with_ending([_ch(100, 180)], _etl(190.0))
    assert [(c.start_s, c.end_s) for c in out] == [(100, 190.0 + ENDING_TAIL_S)]


def test_ending_appended_when_last_chapter_ends_long_before_the_end():
    out = _with_ending([_ch(100, 150)], _etl(300.0))
    assert len(out) == 2 and out[0].end_s == 150
    assert (out[1].title, out[1].kind) == (ENDING_TITLE, "close")
    assert (out[1].start_s, out[1].end_s) == (292.0, 305.0)


def test_ending_appended_after_surrender_style_gap_never_overlaps():
    out = _with_ending([_ch(100, 295)], _etl(300.0 + 30))  # 35 s short: appended
    assert out[1].start_s >= out[0].end_s


def test_ending_unchanged_when_chapter_already_past_the_end():
    chapters = [_ch(100, 400)]
    assert _with_ending(chapters, _etl(300.0)) == chapters


def test_ending_unchanged_without_game_end():
    chapters = [_ch(100, 150)]
    assert _with_ending(chapters, _etl(None)) == chapters


def test_ending_on_an_empty_plan_is_just_the_ending_chapter():
    (c,) = _with_ending([], _etl(300.0))
    assert (c.title, c.start_s, c.end_s) == (ENDING_TITLE, 292.0, 305.0)


def test_plan_from_plays_ends_on_the_nexus():
    g = _game(make_play("a", approach=100, outcome=120))
    plan = _plan(g, _tl(end=500.0))
    assert plan.chapters[-1].title == ENDING_TITLE
    assert plan.chapters[-1].end_s == 505.0
