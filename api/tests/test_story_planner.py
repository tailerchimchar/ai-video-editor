"""Story planner: chapters from Riot fights, expert beats and the lane (pure)."""

from __future__ import annotations

import pytest

from ai_video_editor.league.lane_models import LaneSummary
from ai_video_editor.league.riot_timeline import RiotTimeline, TimelineEvent
from ai_video_editor.story.planner import Beat, plan_story


def _ev(kind: str, t: float) -> TimelineEvent:
    # offset 0: game seconds == recording seconds
    return TimelineEvent(kind, t, t, ["you (Jhin)"], {})


def _tl(events: list[TimelineEvent], game_end_s: float | None = None) -> RiotTimeline:
    evs = list(events)
    if game_end_s is not None:
        evs.append(TimelineEvent("game_end", game_end_s, game_end_s, [], {}))
    return RiotTimeline("NA1_1", "high", 0.0, game_end_s, evs, [])


def _beat(start: float, end: float, text: str, kind: str = "fight") -> Beat:
    return Beat(t_start=start, t_end=end, beat=text, kind=kind)


def test_chapter_widens_to_fight_rules():
    tl = _tl([_ev("kill", 1395.4), _ev("assist", 1409.9)])
    plan = plan_story(tl, [_beat(1395.0, 1410.0, "Neeko pick")], None, mic_stream=1)
    [ch] = plan.chapters
    assert (ch.start_s, ch.end_s) == (1391.4, 1412.9)
    assert ch.title == "Neeko pick"
    assert ch.beats == ["Neeko pick"]
    assert ch.kind == "fight"


def test_ally_death_opening_gets_8s():
    tl = _tl([_ev("ally_death", 1000.0), _ev("kill", 1005.0)])
    plan = plan_story(tl, [_beat(1000.0, 1005.0, "The terrible fight")], None, mic_stream=1)
    [ch] = plan.chapters
    assert (ch.start_s, ch.end_s) == (992.0, 1008.0)


def test_close_chapter_ends_at_game_end_plus_1():
    tl = _tl([_ev("kill", 1800.0), _ev("kill", 1810.0)], game_end_s=1830.0)
    plan = plan_story(tl, [], None, mic_stream=1)
    [ch] = plan.chapters
    assert ch.kind == "close"
    assert (ch.start_s, ch.end_s) == (1796.0, 1831.0)


def test_close_chapter_keeps_its_beat_but_is_titled_the_close():
    tl = _tl([_ev("kill", 1800.0)], game_end_s=1820.0)
    plan = plan_story(tl, [_beat(1799.0, 1815.0, "Nexus falls")], None, mic_stream=1)
    [ch] = plan.chapters
    assert ch.kind == "close"
    assert ch.title == "The close"
    assert ch.beats == ["Nexus falls"]
    assert ch.end_s == 1821.0


def test_beats_in_same_fight_merge_into_one_chapter():
    tl = _tl([_ev("kill", 1200.0), _ev("kill", 1210.0), _ev("assist", 1220.0)])
    beats = [_beat(1199.0, 1205.0, "Flash forward"), _beat(1215.0, 1222.0, "Cleanup")]
    plan = plan_story(tl, beats, None, mic_stream=1)
    [ch] = plan.chapters
    assert (ch.start_s, ch.end_s) == (1196.0, 1223.0)
    assert ch.title == "Flash forward"
    assert ch.beats == ["Flash forward", "Cleanup"]


def test_lane_chapter_from_first_two_events_before_14min():
    tl = _tl([_ev("kill", 146.8), _ev("death", 362.0), _ev("kill", 500.0), _ev("death", 900.0)])
    lane = LaneSummary(opponent_champion="Caitlyn")
    plan = plan_story(tl, [], lane, mic_stream=1)
    assert [c.kind for c in plan.chapters] == ["lane", "lane"]
    first, second = plan.chapters
    assert (first.start_s, first.end_s) == (142.8, 149.8)
    assert (second.start_s, second.end_s) == (354.0, 365.0)
    assert all("Caitlyn" in c.title for c in plan.chapters)


def test_chapters_are_chronological_and_titles_capped_at_40():
    tl = _tl([_ev("kill", 1500.0), _ev("kill", 1100.0)])
    long_text = "A very long beat sentence that goes on well past forty characters"
    beats = [_beat(1500.0, 1501.0, long_text), _beat(1100.0, 1101.0, "Early")]
    plan = plan_story(tl, beats, None, mic_stream=1)
    assert plan.chapters[0].title == "Early"
    assert len(plan.chapters[1].title) <= 40


def test_total_trimmed_to_max_by_dropping_lowest_intensity_non_close_chapter():
    # Fight A: 3 kills (intensity 3); fight B: 1 kill (intensity 1); close fight.
    events = [
        _ev("kill", 1000.0),
        _ev("kill", 1005.0),
        _ev("assist", 1010.0),
        _ev("kill", 1300.0),
        _ev("kill", 1800.0),
    ]
    tl = _tl(events, game_end_s=1810.0)
    beats = [_beat(1000.0, 1010.0, "Big fight"), _beat(1300.0, 1300.0, "Small pick")]
    full = plan_story(tl, beats, None, mic_stream=1, max_s=400.0)
    assert len(full.chapters) == 3
    trimmed = plan_story(tl, beats, None, mic_stream=1, max_s=full.total_s - 1.0)
    assert [c.title for c in trimmed.chapters] == ["Big fight", "The close"]
    assert trimmed.total_s == pytest.approx(sum(c.end_s - c.start_s for c in trimmed.chapters))


def test_trim_never_drops_lane_or_close():
    tl = _tl([_ev("kill", 146.8), _ev("kill", 1800.0)], game_end_s=1810.0)
    plan = plan_story(tl, [], None, mic_stream=1, max_s=1.0)
    assert [c.kind for c in plan.chapters] == ["lane", "close"]


def test_audio_mixed_when_no_mic():
    tl = _tl([_ev("kill", 1000.0)])
    assert plan_story(tl, [], None, mic_stream=None).audio == "mixed"
    plan = plan_story(tl, [], None, mic_stream=3)
    assert plan.audio == "mic_over_game"
    assert plan.mic_stream == 3


# --- review round 1 fixes ---


def test_close_chapter_ignores_fight_far_before_game_end():
    # A fight 4 min before the nexus is not the close: the chapter is the end alone.
    tl = _tl([_ev("kill", 1560.0)], game_end_s=1800.0)
    plan = plan_story(tl, [], None, mic_stream=1)
    [ch] = plan.chapters
    assert ch.kind == "close"
    assert (ch.start_s, ch.end_s) == (1792.0, 1801.0)


def test_close_chapter_uses_fight_ending_within_reach():
    tl = _tl([_ev("kill", 1750.0), _ev("kill", 1760.0)], game_end_s=1800.0)
    [ch] = plan_story(tl, [], None, mic_stream=1).chapters
    assert (ch.start_s, ch.end_s) == (1746.0, 1801.0)


def test_close_chapter_capped_at_60s():
    kills = [_ev("kill", 1700.0 + 10 * i) for i in range(10)]  # one fight 1700-1790
    tl = _tl(kills, game_end_s=1800.0)
    [ch] = plan_story(tl, [], None, mic_stream=1).chapters
    assert (ch.start_s, ch.end_s) == (1741.0, 1801.0)


def test_over_budget_flagged_when_protected_chapters_exceed_max():
    tl = _tl([_ev("kill", 146.8), _ev("kill", 1795.0)], game_end_s=1800.0)
    assert plan_story(tl, [], None, mic_stream=1).over_budget is False
    plan = plan_story(tl, [], None, mic_stream=1, max_s=5.0)
    assert plan.over_budget is True
    assert plan.total_s > 5.0


def test_chapter_starting_past_game_end_is_dropped():
    tl = _tl([_ev("kill", 1795.0)], game_end_s=1800.0)
    plan = plan_story(tl, [_beat(2100.0, 2110.0, "Double-shifted beat")], None, mic_stream=1)
    assert [c.kind for c in plan.chapters] == ["close"]


def test_chapter_is_union_of_beat_and_overlapping_fights():
    # Two fights (gap 30 s > 15 s) under one beat that runs past both.
    tl = _tl([_ev("kill", 1000.0), _ev("kill", 1030.0)])
    [ch] = plan_story(tl, [_beat(1000.0, 1040.0, "Long skirmish")], None, mic_stream=1).chapters
    assert (ch.start_s, ch.end_s) == (996.0, 1040.0)


def test_title_strips_label_prefix():
    tl = _tl([_ev("kill", 1000.0)])
    beats = [_beat(1000.0, 1001.0, "setup: early skirmish forming")]
    [ch] = plan_story(tl, beats, None, mic_stream=1).chapters
    assert ch.title == "early skirmish forming"
    assert ch.beats == ["setup: early skirmish forming"]


def test_mic_stream_zero_is_refused():
    tl = _tl([_ev("kill", 1000.0)])
    plan = plan_story(tl, [], None, mic_stream=0)
    assert plan.mic_stream is None
    assert plan.audio == "mixed"


def test_beat_end_before_start_rejected():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Beat(t_start=10.0, t_end=5.0, beat="backwards")


# --- final fix wave: titles (I2) ---

# Real gaming-expert beat strings from the 2026-11-20 Jhin round.
_JHIN_FIRST_BLOOD = [
    "setup: early skirmish forming around the 2.5 minute mark, level 2, no event yet",
    "payoff: Jhin takes the first kill of the game on the enemy MissFortune with JarvanIV "
    "and Galio assisting",
    "aftermath: fight continues, no further Jhin event",
    "cost: ally Viktor dies to enemy Sion, so the kill is traded back",
    "tail: no events, window overruns the payoff by about 14 s",
]


def test_clean_title_strips_any_short_label_and_cuts_at_word_boundary():
    from ai_video_editor.story.planner import clean_title

    assert (
        clean_title("lane/map state: team looks stressed on comms about losing control; no event")
        == "team looks stressed on comms about"
    )
    t = clean_title(_JHIN_FIRST_BLOOD[1])
    assert t == "Jhin takes the first kill of the game"
    assert len(t) <= 40 and not t.endswith(" ")
    assert clean_title("decision/payoff 1: Jhin kills the enemy Neeko with Viktor and Galio") == (
        "Jhin kills the enemy Neeko"
    )


def test_title_prefers_payoff_and_never_uses_tail_aftermath_cost_or_no_event():
    # Same beats shifted past the lane phase (a lane chapter is titled "Lane").
    tl = _tl([_ev("kill", 1145.8), _ev("ally_death", 1151.8)])
    beats = [
        _beat(1137.0, 1145.8, _JHIN_FIRST_BLOOD[0]),
        _beat(1145.8, 1147.8, _JHIN_FIRST_BLOOD[1]),
        _beat(1147.8, 1150.8, _JHIN_FIRST_BLOOD[2]),
        _beat(1150.8, 1151.8, _JHIN_FIRST_BLOOD[3]),
        _beat(1151.8, 1165.1, _JHIN_FIRST_BLOOD[4]),
    ]
    [ch] = plan_story(tl, beats, None, mic_stream=1).chapters
    assert ch.title == "Jhin takes the first kill of the game"


def test_title_falls_back_when_only_excluded_beats():
    tl = _tl([_ev("kill", 1000.0)])
    beats = [
        _beat(999.0, 1001.0, "aftermath: fight resolved, team reacts"),
        _beat(1000.0, 1001.0, "cost: ally Viktor dies to enemy Sion"),
    ]
    [ch] = plan_story(tl, beats, None, mic_stream=1).chapters
    assert "aftermath" not in ch.title.lower() and "viktor" not in ch.title.lower()
    assert ch.title == "The fight"


def test_close_chapter_is_always_titled_the_close():
    tl = _tl([_ev("kill", 1800.0)], game_end_s=1820.0)
    plan = plan_story(tl, [_beat(1799.0, 1815.0, "payoff: Nexus falls")], None, mic_stream=1)
    [ch] = plan.chapters
    assert ch.kind == "close" and ch.title == "The close"


def test_lane_chapter_title_is_lane_vs_champion():
    tl = _tl([_ev("kill", 146.8)])
    lane = LaneSummary(opponent_champion="MissFortune")
    [ch] = plan_story(tl, [], lane, mic_stream=1).chapters
    assert ch.title == "Lane vs MissFortune"
    [ch] = plan_story(tl, [], None, mic_stream=1).chapters
    assert ch.title == "Lane"


# --- final fix wave: chapter selection (I6d, M3, M4) ---


def _jhin_tl() -> RiotTimeline:
    """Owner-relevant events from the real Jhin timeline around 9-10 and 29-30 min."""
    return _tl(
        [
            _ev("assist", 568.0),
            _ev("ally_death", 568.1),
            _ev("enemy_death", 1796.5),
            _ev("assist", 1797.7),
            _ev("assist", 1799.9),
            _ev("kill", 1820.1),
        ],
        game_end_s=2037.568,
    )


def test_beat_chapter_without_a_riot_event_is_dropped():
    # 02_hype_callout_9m45s: "no Riot event in 585-620 s".
    beat = _beat(
        585.0,
        620.0,
        "no Riot event in 585-620 s; players discuss an enemy shutdown and a possible 5v5",
    )
    plan = plan_story(_jhin_tl(), [beat], None, mic_stream=1)
    assert all(c.start_s > 700 for c in plan.chapters)


def test_fight_chapter_capped_at_45s_keeping_the_payoff_end():
    # 08_assist_29m30s (1770-1808.5) + 09_kill_30m07s (1807.5-1836): was 1770-1836 (66 s).
    b08, b09 = 1770.0, 1807.5
    beats = [
        _beat(b08, b08 + 26.5, "lane/map state: team looks stressed on comms; no event"),
        _beat(b08 + 26.5, b08 + 27.7, "opening: MissFortune dies to JarvanIV, Viktor"),
        _beat(b08 + 27.7, b08 + 29.9, "payoff: Jhin assists on Neeko, then on Gangplank"),
        _beat(b08 + 29.9, b08 + 38.5, "aftermath: team reaction; no further event"),
        _beat(b09, b09 + 12.6, "setup: Jhin moves up"),
        _beat(b09 + 12.6, b09 + 13.6, "payoff: Jhin kills the enemy Rumble alone"),
        _beat(b09 + 13.6, b09 + 28.5, "tail: no later events"),
    ]
    plan = plan_story(_jhin_tl(), beats, None, mic_stream=1)
    fights_ = [c for c in plan.chapters if c.kind == "fight"]
    [ch] = fights_
    assert ch.end_s == 1823.1  # Rumble kill + FIGHT_POST_S; the eventless tail is gone
    assert ch.end_s - ch.start_s == pytest.approx(45.0)
    assert ch.title == "Jhin assists on Neeko, then on Gangplank"


def test_close_chapter_is_not_capped_at_45():
    kills = [_ev("kill", 1700.0 + 10 * i) for i in range(10)]
    [ch] = plan_story(_tl(kills, game_end_s=1800.0), [], None, mic_stream=1).chapters
    assert ch.end_s - ch.start_s == pytest.approx(60.0)


def test_lane_chapter_widens_to_its_fight():
    tl = _tl([_ev("kill", 146.8), _ev("ally_death", 152.0), _ev("assist", 158.0)])
    [ch] = plan_story(tl, [], None, mic_stream=1).chapters
    assert ch.kind == "lane"
    assert (ch.start_s, ch.end_s) == (142.8, 161.0)


def test_default_max_is_360_and_cards_count_toward_total():
    import inspect

    assert inspect.signature(plan_story).parameters["max_s"].default == 360.0
    assert "target_s" not in inspect.signature(plan_story).parameters
    tl = _tl([_ev("kill", 1000.0), _ev("kill", 1300.0)])
    beats = [_beat(1000.0, 1001.0, "payoff: A"), _beat(1300.0, 1301.0, "payoff: B")]
    bare = plan_story(tl, beats, None, mic_stream=1)
    carded = plan_story(tl, beats, None, mic_stream=1, card_s=2.5)
    assert carded.total_s == pytest.approx(bare.total_s + 2 * 2.5)
    # Cards push the total over max: the weaker chapter is dropped.
    tight = plan_story(tl, beats, None, mic_stream=1, card_s=2.5, max_s=bare.total_s)
    assert len(tight.chapters) == 1


def test_beat_rounded_to_a_tenth_still_catches_its_kill():
    # Real Jhin 09 payoff: beat 1820.1-1821.1, Riot kill at 1820.07 s.
    tl = _tl([_ev("kill", 1820.07)])
    beats = [_beat(1807.5, 1820.1, "setup: Jhin moves up"),
             _beat(1820.1, 1821.1, "payoff: Jhin kills the enemy Rumble alone")]  # fmt: skip
    [ch] = plan_story(tl, beats, None, mic_stream=1).chapters
    assert ch.title == "Jhin kills the enemy Rumble alone"
