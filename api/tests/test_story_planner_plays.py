"""plan_story_from_plays: chapters 1:1 from owner-involved plays."""

from __future__ import annotations

from _play_factory import make_play

from ai_video_editor.league.play_models import GameModel
from ai_video_editor.league.riot_timeline import RiotTimeline, TimelineEvent
from ai_video_editor.story.planner import plan_story_from_plays


def _tl(kills: list[float] | None = None, end: float | None = 2000.0) -> RiotTimeline:
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
    assert _plan(g).chapters[0].end_s == 2001.0


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
    assert plan.chapters[0].kill_times_s == [106.5]  # 119 + 1.5 falls past the outcome


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
