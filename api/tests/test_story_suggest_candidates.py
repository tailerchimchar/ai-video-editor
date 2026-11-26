"""Candidate builders: the per-game registry and the League builder."""

from __future__ import annotations

import pytest
from _play_factory import make_play

from ai_video_editor.league.play_models import GameModel
from ai_video_editor.league.riot_timeline import RiotTimeline, TimelineEvent
from ai_video_editor.story.planner import Chapter, StoryPlan
from ai_video_editor.story.suggest.candidates import (
    UnknownGameError,
    candidate_builder,
    register_candidate_builder,
)
from ai_video_editor.story.suggest.league_candidates import LeagueCandidateBuilder

OFFSET = 30.0  # recording seconds = game clock + 30


def _ev(kind, t_game, actors=(), detail=None):
    return TimelineEvent(kind, t_game, t_game + OFFSET, list(actors), detail or {})


def _timeline(events) -> RiotTimeline:
    return RiotTimeline("m", "high", OFFSET, 2030.0, events, [], owner_team_id=100)


def _chapter(start_game, end_game, kind="fight", play_id=None) -> Chapter:
    return Chapter(
        title="t",
        start_s=start_game + OFFSET,
        end_s=end_game + OFFSET,
        kind=kind,
        play_id=play_id,
    )


def _plan(*chapters) -> StoryPlan:
    return StoryPlan(
        asset_id="a1", chapters=list(chapters), total_s=100.0, mic_stream=None, audio="mixed"
    )


def _builder(game: GameModel | None = None) -> LeagueCandidateBuilder:
    return LeagueCandidateBuilder(load_plays=lambda asset_id: game)


EVENTS = [
    # lane: a solo kill at 3:00
    _ev("kill", 180.0, ["you (Zeri)", "enemy Ashe"]),
    # mid fight 15:00-15:08: two owner kills 4 s apart, an ally death
    _ev("ally_death", 898.0, ["enemy Diana", "ally Thresh"]),
    _ev("kill", 900.0, ["you (Zeri)", "enemy Diana"]),
    _ev("kill", 904.0, ["you (Zeri)", "enemy Urgot", "ally Thresh"]),
    _ev("elite", 920.0, ["ally Vi"], {"monsterType": "DRAGON", "killerTeamId": 100}),
    # a fight the owner had no part in, inside no chapter
    _ev("ally_death", 1200.0, ["enemy Ashe", "ally Vi"]),
    # the close: owner dies then teammates win 4v5
    _ev("death", 1980.0, ["enemy Ashe", "you (Zeri)"]),
    _ev("assist", 1985.0, ["ally Vi", "enemy Ashe", "you (Zeri)"]),
]


def test_league_builder_one_candidate_per_owner_fight():
    plan = _plan(
        _chapter(170, 200, kind="early"),
        _chapter(890, 930, play_id="p1"),
        _chapter(1970, 2001, kind="close", play_id="p2"),
    )
    game = GameModel(
        plays=[
            make_play("p1", first_kill=900, last_kill=904, numbers="5v5", outcome_result="won"),
            make_play(
                "p2",
                type="closeout",
                first_kill=1980,
                last_kill=1985,
                numbers="4v5",
                outcome_result="won",
            ),
        ]
    )
    cands = _builder(game).build(plan, _timeline(EVENTS))
    assert [c.chapter_index for c in cands] == [0, 1, 2]

    lane, mid, close = cands
    assert lane.facts["minute"] == 3
    assert lane.facts["kills"] == 1 and lane.facts["multikill"] == 1
    assert lane.facts["enemy_champions"] == ["Ashe"]
    assert lane.anchor_s == 180.0
    assert lane.facts["numbers"] is None and lane.facts["outnumbered_win"] is False

    assert mid.facts["kills"] == 2 and mid.facts["multikill"] == 2
    assert mid.facts["enemy_champions"] == ["Diana", "Urgot"]
    assert mid.facts["objective"] == "dragon"
    assert mid.facts["numbers"] == "5v5" and mid.facts["outnumbered_win"] is False
    assert mid.facts["is_last_fight"] is False

    assert close.facts["deaths"] == 1 and close.facts["kills"] == 0
    assert close.facts["outnumbered_win"] is True and close.facts["numbers"] == "4v5"
    assert close.facts["is_last_fight"] is True
    assert close.anchor_s is None


def test_candidate_spans_are_game_clock_inside_the_chapter():
    plan = _plan(_chapter(899, 930, play_id="p1"))
    (c,) = _builder().build(plan, _timeline(EVENTS))
    assert 899.0 <= c.start_s < c.end_s <= 930.0
    assert c.start_s <= 900.0 and c.end_s >= 904.0


def test_facts_keys_are_exactly_the_contract_and_carry_no_text():
    plan = _plan(_chapter(890, 930))
    (c,) = _builder().build(plan, _timeline(EVENTS))
    assert set(c.facts) == {
        "minute",
        "kills",
        "deaths",
        "numbers",
        "outnumbered_win",
        "multikill",
        "objective",
        "is_last_fight",
        "enemy_champions",
    }
    assert "you" not in str(c.facts) and "ally" not in str(c.facts)


def test_chapter_without_owner_events_gives_nothing():
    plan = _plan(_chapter(1190, 1210))
    assert _builder().build(plan, _timeline(EVENTS)) == []


def test_multikill_breaks_on_gaps_over_ten_seconds():
    events = [_ev("kill", t, ["you (Zeri)", "enemy Ashe"]) for t in (500.0, 509.0, 514.0)]
    events.append(_ev("kill", 525.0, ["you (Zeri)", "enemy Ashe"]))  # 11 s after: new chain
    plan = _plan(_chapter(495, 530))
    (c,) = _builder().build(plan, _timeline(events))
    assert c.facts["kills"] == 4 and c.facts["multikill"] == 3


def test_registry_unknown_game_and_league_default():
    assert isinstance(candidate_builder("league"), LeagueCandidateBuilder)
    with pytest.raises(UnknownGameError):
        candidate_builder("bowling")


def test_registering_a_new_game_needs_no_edits():
    class Stub:
        def build(self, plan, timeline):
            return []

    register_candidate_builder("test_game", Stub())
    assert isinstance(candidate_builder("test_game"), Stub)
