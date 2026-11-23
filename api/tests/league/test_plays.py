"""Riot play proposer on invented 10-participant matches (game understanding, Task 1)."""

import json
from pathlib import Path

from ai_video_editor.league.phases import phases_for
from ai_video_editor.league.play_models import GameModel
from ai_video_editor.league.plays import play_text, propose_plays, rank_plays
from ai_video_editor.league.riot_timeline import parse_timeline

# Blue (100): pids 1-5, red (200): pids 6-10. Owner is pid 1 unless stated.
_CHAMPS = ["Jhin", "Thresh", "Lux", "Garen", "Ahri", "Diana", "Ezreal", "Zed", "Vi", "Karma"]
GAME_END_S = 2000


def _match() -> dict:
    parts = [
        {
            "puuid": f"P{i + 1}",
            "participantId": i + 1,
            "championName": c,
            "teamId": 100 if i < 5 else 200,
        }
        for i, c in enumerate(_CHAMPS)
    ]
    return {
        "metadata": {"matchId": "NA1_PLAYS"},
        "info": {
            "gameStartTimestamp": 1_000,
            "gameEndTimestamp": 1_000 + GAME_END_S * 1000,
            "participants": parts,
        },
    }


def _ck(t_s: float, killer: int, victim: int, assists=None, pos=None) -> dict:
    ev = {
        "type": "CHAMPION_KILL",
        "timestamp": int(t_s * 1000),
        "killerId": killer,
        "victimId": victim,
        "assistingParticipantIds": assists or [],
    }
    if pos is not None:
        ev["position"] = {"x": pos[0], "y": pos[1]}
    return ev


def _dragon(t_s: float, team: int = 100) -> dict:
    return {
        "type": "ELITE_MONSTER_KILL",
        "timestamp": int(t_s * 1000),
        "killerId": 2,
        "killerTeamId": team,
        "monsterType": "DRAGON",
        "monsterSubType": "AIR_DRAGON",
    }


def _build(events: list[dict], owner: str = "P1"):
    timeline = {"info": {"frames": [{"timestamp": 0, "events": events}]}}
    tl = parse_timeline(_match(), timeline, owner_puuid=owner, offset_s=0.0, confidence="high")
    phases = phases_for(tl)
    return tl, propose_plays(tl, phases=phases)


MID = (8000, 7800)


def test_teamfight_with_sides_numbers_and_initiator():
    evs = [
        _ck(1000, 1, 6, [2, 3], MID),
        _ck(1003, 4, 7, [5], MID),
        _ck(1006, 8, 2, [9], MID),
        _ck(1009, 3, 8, [1], MID),
        _ck(1012, 5, 9, [4], MID),
    ]
    _, plays = _build(evs)
    p = plays[0]
    assert p.id == "p01"
    assert p.type == "teamfight"
    assert p.sides.numbers == "5v4"
    assert p.sides.numbers_source == "riot_lower_bound"
    assert p.owner.role == "initiator_kill"
    assert p.result.outcome == "won"
    assert (p.result.enemy_deaths, p.result.ally_deaths) == (4, 1)
    assert p.window.source == "vlm_pending"
    assert p.zone == "mid_lane"
    assert p.flags.help_from_team is True


def test_objective_fight_from_elite_after():
    evs = [_ck(1000, 1, 6, [2], MID), _ck(1004, 2, 7, [1], MID), _dragon(1016)]
    _, plays = _build(evs)
    assert plays[0].type == "objective_fight"
    assert plays[0].result.objective_after == ["DRAGON/AIR_DRAGON"]
    assert plays[0].flags.steal is False


def test_steal_flag_when_other_team_takes_objective():
    evs = [_ck(1000, 1, 6, [2, 3], MID), _ck(1004, 4, 7, [1], MID), _dragon(1016, team=200)]
    _, plays = _build(evs)
    assert plays[0].flags.steal is True


def test_help_from_team_true_for_owner_plus_one_ally():
    _, plays = _build([_ck(1000, 1, 6, [2], MID)])
    assert plays[0].flags.help_from_team is True


def test_help_from_team_false_for_owner_alone_and_for_single_absent_ally():
    _, alone = _build([_ck(1000, 1, 6, [], MID)])
    assert alone[0].flags.help_from_team is False
    _, absent = _build([_ck(1000, 2, 6, [], MID)])
    assert absent[0].owner.role == "absent"
    assert absent[0].flags.help_from_team is False


def test_closeout_near_game_end():
    t = GAME_END_S - 40
    evs = [_ck(t, 1, 6, [2], MID), _ck(t + 3, 2, 7, [1], MID), _ck(t + 6, 1, 8, [], MID)]
    _, plays = _build(evs)
    assert plays[0].type == "closeout"
    assert plays[0].phase == "late"


def test_gank_early_three_champions_in_lane():
    _, plays = _build([_ck(300, 2, 6, [3], (12800, 1500))])
    assert plays[0].type == "gank"
    assert plays[0].phase == "early"


def test_caught_out_single_ally_death():
    _, plays = _build([_ck(1000, 7, 1, [], (8000, 4500))])
    p = plays[0]
    assert p.type == "caught_out"
    assert p.owner.role == "caught_first"
    assert p.owner.died is True


def test_dive_near_tower():
    _, plays = _build([_ck(1000, 7, 2, [8], (10400, 1100))])
    assert plays[0].type == "dive"


def test_absent_owner_play_is_kept_and_tagged():
    _, plays = _build([_ck(1000, 2, 6, [3], MID), _ck(1004, 3, 7, [2], MID)])
    assert plays[0].owner.role == "absent"
    assert plays[0].owner.involved is False


def test_zone_relative_flips_for_red_side_owner():
    # (9000, 3000) is blue_bot_jungle; owner pid 6 is red.
    evs = [_ck(1000, 6, 1, [7], (9000, 3000)), _ck(1004, 7, 2, [6], (9000, 3000))]
    tl, plays = _build(evs, owner="P6")
    assert tl.owner_team_id == 200
    assert plays[0].zone == "blue_bot_jungle"
    assert plays[0].zone_relative == "enemy_jungle"
    _, blue = _build(evs, owner="P1")
    assert blue[0].zone_relative == "ally_jungle"


def test_kill_without_position_gets_unknown_zone():
    _, plays = _build([_ck(1000, 1, 6, [2]), _ck(1004, 2, 7, [1])])
    assert plays[0].zone == "unknown"
    assert plays[0].position is None


def test_play_text_template():
    evs = [
        _ck(1000, 1, 6, [2, 3], MID),
        _ck(1003, 4, 7, [5], MID),
        _ck(1006, 8, 2, [9], MID),
        _ck(1009, 3, 8, [1], MID),
        _ck(1012, 5, 9, [4], MID),
        _dragon(1020),
    ]
    _, plays = _build(evs)
    assert play_text(plays[0]) == (
        "5v4 teamfight in mid_lane, owner initiator_kill, won 4-for-1, then DRAGON/AIR_DRAGON"
    )
    plays[0].result.objective_after = []
    assert play_text(plays[0]).endswith("won 4-for-1")


def test_rank_plays_orders_absent_last_then_numbers_faced():
    evs = [
        _ck(300, 2, 6, [3], MID),  # absent
        _ck(600, 1, 7, [2], MID),
        _ck(900, 1, 8, [2], MID),
        _ck(905, 1, 9, [2], MID),
    ]
    _, plays = _build(evs)
    assert [p.owner.role for p in plays] == ["absent", "initiator_kill", "initiator_kill"]
    plays[2].impressive.numbers_faced = 3
    ranked = rank_plays(plays)
    assert ranked[-1].owner.role == "absent"
    assert ranked[0].game_time.first_kill_s == 900
    assert [p.impressive.rank for p in ranked] == [1, 2, 3]


def test_example_fixture_validates_as_game_model():
    path = Path(__file__).parent.parent / "fixtures" / "plays" / "jhin_example.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    game = GameModel.model_validate(data["game"])
    assert len(game.plays) == 21
    assert sum(p.owner.involved for p in game.plays) == 13
    p15 = next(p for p in game.plays if p.id == "p15")
    assert p15.type == "teamfight"
    assert p15.sides.numbers == "5v4"
    assert game.phases is not None and game.phases.late is not None
