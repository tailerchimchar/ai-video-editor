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


def test_close_chapter_takes_title_from_its_beat():
    tl = _tl([_ev("kill", 1800.0)], game_end_s=1820.0)
    plan = plan_story(tl, [_beat(1799.0, 1815.0, "Nexus falls")], None, mic_stream=1)
    [ch] = plan.chapters
    assert ch.kind == "close"
    assert ch.title == "Nexus falls"
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
