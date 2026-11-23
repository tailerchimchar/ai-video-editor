"""Event-driven phases from invented timelines (game understanding, Task 1)."""

from ai_video_editor.league.phases import phase_of, phases_for
from ai_video_editor.league.riot_timeline import parse_timeline

from .test_riot_timeline import _ck, _match


def _tl(events: list[dict], end_s: int | None):
    evs = list(events)
    if end_s is not None:
        evs.append({"type": "GAME_END", "timestamp": end_s * 1000, "winningTeam": 100})
    timeline = {"info": {"frames": [{"timestamp": 0, "events": evs}]}}
    return parse_timeline(_match(None), timeline, owner_puuid="ME", offset_s=0.0, confidence="high")


def _tower(t_s: int) -> dict:
    return {
        "type": "BUILDING_KILL",
        "timestamp": t_s * 1000,
        "killerId": 1,
        "teamId": 200,
        "buildingType": "TOWER_BUILDING",
    }


def _baron(t_s: int) -> dict:
    return {
        "type": "ELITE_MONSTER_KILL",
        "timestamp": t_s * 1000,
        "killerId": 1,
        "monsterType": "BARON_NASHOR",
        "killerTeamId": 100,
    }


def test_early_ends_at_first_tower_but_never_before_8_min():
    p = phases_for(_tl([_ck(100_000, 1, 6), _tower(360)], 2000))
    assert p.early.end_s == 480
    assert p.early.boundary == "first_tower"


def test_early_ends_at_first_tower_when_after_8_min():
    p = phases_for(_tl([_tower(600)], 2000))
    assert p.early.end_s == 600


def test_early_ends_at_14_min_without_tower():
    p = phases_for(_tl([_ck(100_000, 1, 6)], 2000))
    assert p.early.end_s == 840
    assert p.early.boundary == "14:00"
    assert p.mid.start_s == 840


def test_late_starts_at_first_baron():
    p = phases_for(_tl([_baron(1463)], 2000))
    assert p.late.start_s == 1463
    assert p.late.boundary == "first_baron"
    assert p.mid.end_s == 1463
    assert p.late.end_s == 2000


def test_late_defaults_to_25_min():
    p = phases_for(_tl([], 2000))
    assert p.late.start_s == 1500
    assert p.late.boundary == "25:00"


def test_surrender_before_late_has_two_phases():
    p = phases_for(_tl([_ck(100_000, 1, 6)], 1100))
    assert p.late is None
    assert p.mid.end_s == 1100


def test_remake_has_no_phases():
    assert phases_for(_tl([], 240)) is None


def test_phase_of():
    p = phases_for(_tl([], 2000))
    assert phase_of(p, 100) == "early"
    assert phase_of(p, 900) == "mid"
    assert phase_of(p, 1600) == "late"
    short = phases_for(_tl([_ck(100_000, 1, 6)], 1100))
    assert phase_of(short, 1600) == "mid"
