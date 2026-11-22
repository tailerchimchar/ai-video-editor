"""Riot timeline parser, fights, cache and drift check (Jhin round, Task 1).

All data here is invented; no network, no paths outside tmp_path."""

import json

import pytest

from ai_video_editor.league.riot_timeline import (
    OwnerFrame,
    TimelineEvent,
    actor_label,
    fights,
    load_or_fetch,
    offset_drift_s,
    parse_timeline,
    pre_pad_for_drift,
)

# Owner is pid 1 (Jhin, blue 100); ally Thresh pid 2; enemies Diana 6, Ezreal 7.
_PARTS = [
    {"puuid": "ME", "participantId": 1, "championName": "Jhin", "teamId": 100},
    {"puuid": "A2", "participantId": 2, "championName": "Thresh", "teamId": 100},
    {"puuid": "E6", "participantId": 6, "championName": "Diana", "teamId": 200},
    {"puuid": "E7", "participantId": 7, "championName": "Ezreal", "teamId": 200},
]


def _match(end_ms: int | None = 1_500_000) -> dict:
    info = {"gameStartTimestamp": 1_000, "participants": [dict(p) for p in _PARTS]}
    if end_ms is not None:
        info["gameEndTimestamp"] = 1_000 + end_ms
    return {"metadata": {"matchId": "NA1_TEST"}, "info": info}


def _pf(gold: int, cs: int, jcs: int, level: int) -> dict:
    return {"totalGold": gold, "minionsKilled": cs, "jungleMinionsKilled": jcs, "level": level}


def _ck(ts: int, killer: int, victim: int, assists: list[int] | None = None) -> dict:
    ev = {"type": "CHAMPION_KILL", "timestamp": ts, "killerId": killer, "victimId": victim}
    if assists is not None:
        ev["assistingParticipantIds"] = assists
    return ev


def _timeline(with_game_end: bool = True) -> dict:
    events_m1 = [
        _ck(600_000, 1, 6, [2]),
        _ck(610_000, 6, 2, [7]),
        _ck(615_000, 2, 7, [1]),
        {
            "type": "ELITE_MONSTER_KILL",
            "timestamp": 700_000,
            "killerId": 2,
            "monsterType": "DRAGON",
            "monsterSubType": "FIRE_DRAGON",
        },
        {"type": "ITEM_PURCHASED", "timestamp": 701_000, "participantId": 1},
    ]
    events_m2 = [
        {
            "type": "BUILDING_KILL",
            "timestamp": 1_490_000,
            "killerId": 1,
            "teamId": 200,
            "buildingType": "TOWER_BUILDING",
            "towerType": "NEXUS_TURRET",
            "laneType": "MID_LANE",
        },
    ]
    if with_game_end:
        events_m2.append({"type": "GAME_END", "timestamp": 1_500_000, "winningTeam": 100})
    return {
        "info": {
            "frames": [
                {"timestamp": 0, "events": [], "participantFrames": {"1": _pf(500, 0, 0, 1)}},
                {
                    "timestamp": 60_010,
                    "events": events_m1,
                    "participantFrames": {"1": _pf(900, 8, 2, 2), "2": _pf(1, 1, 1, 1)},
                },
                {
                    "timestamp": 120_020,
                    "events": events_m2,
                    "participantFrames": {"1": _pf(1400, 15, 4, 3)},
                },
            ]
        }
    }


def _parse(offset: float = 10.0, confidence: str = "high", end_ms=1_500_000, **kw):
    return parse_timeline(
        _match(end_ms),
        _timeline(**kw),
        owner_puuid="ME",
        offset_s=offset,
        confidence=confidence,
    )


# ----- actor labels -----


def test_actor_label_renders_owner_ally_enemy():
    parts = {
        1: {"championName": "Jhin", "teamId": 100},
        2: {"championName": "Thresh", "teamId": 100},
        6: {"championName": "Diana", "teamId": 200},
    }
    assert actor_label(1, parts, 1) == "you (Jhin)"
    assert actor_label(2, parts, 1) == "ally Thresh"
    assert actor_label(6, parts, 1) == "enemy Diana"
    assert actor_label(0, parts, 1) == "unknown"


# ----- parse_timeline -----


def test_parse_classifies_champion_kills_relative_to_owner():
    tl = _parse()
    kinds = [(e.kind, e.t_game_s) for e in tl.events]
    assert ("kill", 600.0) in kinds
    assert ("ally_death", 610.0) in kinds
    assert ("assist", 615.0) in kinds
    kill = next(e for e in tl.events if e.kind == "kill")
    assert kill.actors == ["you (Jhin)", "enemy Diana", "ally Thresh"]
    assert kill.t_rec_s == 610.0  # game 600 + offset 10


def test_parse_enemy_death_when_owner_not_involved():
    timeline = {"info": {"frames": [{"timestamp": 0, "events": [_ck(5_000, 2, 6)]}]}}
    tl = parse_timeline(_match(), timeline, owner_puuid="ME", offset_s=0.0, confidence="high")
    assert [e.kind for e in tl.events if e.kind != "game_end"] == ["enemy_death"]


def test_parse_building_elite_and_game_end():
    tl = _parse()
    building = next(e for e in tl.events if e.kind == "building")
    assert building.detail["buildingType"] == "TOWER_BUILDING"
    assert building.detail["towerType"] == "NEXUS_TURRET"
    assert building.detail["laneType"] == "MID_LANE"
    elite = next(e for e in tl.events if e.kind == "elite")
    assert elite.detail["monsterType"] == "DRAGON"
    assert elite.actors == ["ally Thresh"]
    end = next(e for e in tl.events if e.kind == "game_end")
    assert end.t_game_s == 1500.0
    assert tl.game_end_s == 1510.0
    assert tl.match_id == "NA1_TEST"
    assert tl.confidence == "high"
    assert tl.offset_s == 10.0
    assert tl.pre_pad_s == 4.0
    # unknown event types are ignored; events are chronological
    assert [e.t_game_s for e in tl.events] == sorted(e.t_game_s for e in tl.events)
    assert len(tl.events) == 6


def test_parse_game_end_falls_back_to_match_timestamps():
    tl = _parse(with_game_end=False)
    assert tl.game_end_s == 1500.0 + 10.0
    assert [e.kind for e in tl.events].count("game_end") == 1


def test_parse_game_end_none_when_unknown():
    tl = _parse(with_game_end=False, end_ms=None)
    assert tl.game_end_s is None
    assert all(e.kind != "game_end" for e in tl.events)


def test_parse_owner_frames():
    tl = _parse()
    assert tl.owner_frames == [
        OwnerFrame(minute=0, gold=500, cs=0, level=1),
        OwnerFrame(minute=1, gold=900, cs=10, level=2),
        OwnerFrame(minute=2, gold=1400, cs=19, level=3),
    ]


def test_parse_unknown_owner_raises():
    with pytest.raises(ValueError):
        parse_timeline(_match(), _timeline(), owner_puuid="NOPE", offset_s=0.0, confidence="high")


# ----- fights -----


def _ev(kind: str, t: float) -> TimelineEvent:
    return TimelineEvent(kind=kind, t_game_s=t, t_rec_s=t, actors=[], detail={})


def test_fights_chain_within_gap_and_split_beyond():
    evs = [
        _ev("kill", 100.0),
        _ev("ally_death", 110.0),
        _ev("assist", 124.0),  # 14 s after previous -> same fight
        _ev("enemy_death", 140.0),  # 16 s -> new fight
        _ev("death", 150.0),
    ]
    out = fights(evs)
    assert [(f.start_s, f.end_s) for f in out] == [(100.0, 124.0), (140.0, 150.0)]
    assert len(out[0].events) == 3


def test_fights_ignore_non_fight_kinds():
    evs = [
        _ev("elite", 95.0),
        _ev("kill", 100.0),
        _ev("building", 110.0),
        _ev("game_end", 112.0),
        _ev("death", 120.0),  # 20 s after the kill; building must not bridge
    ]
    out = fights(evs)
    assert [(f.start_s, f.end_s) for f in out] == [(100.0, 100.0), (120.0, 120.0)]


def test_fights_custom_gap_and_unsorted_input():
    out = fights([_ev("kill", 130.0), _ev("kill", 100.0)], gap_s=40.0)
    assert [(f.start_s, f.end_s) for f in out] == [(100.0, 130.0)]


# ----- cache -----


def _fetcher(confidence: str, calls: list):
    def fetch():
        calls.append(1)
        return (_match(), _timeline(), "NA1_TEST", confidence, 12.5, "ME")

    return fetch


def test_load_or_fetch_round_trip(tmp_path):
    calls: list = []
    tl = load_or_fetch("asset-1", fetch=_fetcher("high", calls), cache_dir=tmp_path)
    assert tl is not None and tl.offset_s == 12.5 and tl.confidence == "high"
    meta = json.loads((tmp_path / "meta.json").read_text())
    assert meta["match_id"] == "NA1_TEST"
    assert meta["confidence"] == "high"
    assert meta["offset_s"] == 12.5
    assert meta["owner_puuid"] == "ME"
    assert "fetched_at" in meta
    assert (tmp_path / "match.json").exists() and (tmp_path / "timeline.json").exists()

    again = load_or_fetch("asset-1", fetch=_fetcher("high", calls), cache_dir=tmp_path)
    assert len(calls) == 1  # served from disk
    assert again == tl


def test_load_or_fetch_never_reuses_low(tmp_path):
    calls: list = []
    first = load_or_fetch("asset-1", fetch=_fetcher("low", calls), cache_dir=tmp_path)
    assert first is not None and first.confidence == "low"
    load_or_fetch("asset-1", fetch=_fetcher("low", calls), cache_dir=tmp_path)
    assert len(calls) == 2


def test_load_or_fetch_none_when_fetch_fails(tmp_path):
    assert load_or_fetch("asset-1", fetch=lambda: None, cache_dir=tmp_path) is None


def test_load_or_fetch_default_dir_under_workspace(tmp_path, monkeypatch):
    from ai_video_editor.config import settings

    monkeypatch.setattr(settings, "workspace_dir", tmp_path)
    load_or_fetch("asset-9", fetch=_fetcher("medium", []))
    assert (tmp_path / "riot" / "asset-9" / "meta.json").exists()


def test_load_or_fetch_five_tuple_derives_owner_from_riot_id(tmp_path, monkeypatch):
    from ai_video_editor.config import settings

    monkeypatch.setattr(settings, "riot_id", "Noodle#NA1")
    match = _match()
    match["info"]["participants"][0].update({"riotIdGameName": "Noodle", "riotIdTagline": "NA1"})
    tl = load_or_fetch(
        "a", fetch=lambda: (match, _timeline(), "NA1_TEST", "high", 0.0), cache_dir=tmp_path
    )
    assert tl is not None and any(e.kind == "kill" for e in tl.events)


def test_record_offset_updates_cached_meta(tmp_path):
    from ai_video_editor.league.riot_timeline import record_offset

    load_or_fetch("asset-1", fetch=_fetcher("high", []), cache_dir=tmp_path)
    record_offset("asset-1", 33.0, cache_dir=tmp_path)
    tl = load_or_fetch("asset-1", fetch=lambda: None, cache_dir=tmp_path)
    assert tl is not None and tl.offset_s == 33.0
    kill = next(e for e in tl.events if e.kind == "kill")
    assert kill.t_rec_s == 633.0


def test_record_offset_noop_without_cache(tmp_path):
    from ai_video_editor.league.riot_timeline import record_offset

    record_offset("asset-1", 33.0, cache_dir=tmp_path / "missing")
    assert not (tmp_path / "missing").exists()


# ----- drift -----


def test_offset_drift_uses_both_halves():
    seen = []

    def align(rng):
        seen.append(rng)
        return 10.0 if rng[0] == 0.0 else 14.5

    assert offset_drift_s(align, duration_s=1000.0) == pytest.approx(4.5)
    assert seen == [(0.0, 500.0), (500.0, 1000.0)]


def test_drift_over_three_widens_pre_pad():
    assert pre_pad_for_drift(3.5) == 8.0
    assert pre_pad_for_drift(3.0) == 4.0
    assert pre_pad_for_drift(0.0) == 4.0


# ----- Fix round 1: cache validity, sync_only, gameDuration fallback -----


def _seed(tmp_path, **meta_overrides):
    load_or_fetch("a", fetch=_fetcher("high", []), cache_dir=tmp_path)
    meta = json.loads((tmp_path / "meta.json").read_text())
    for k, v in meta_overrides.items():
        if v is None:
            meta.pop(k, None)
        else:
            meta[k] = v
    (tmp_path / "meta.json").write_text(json.dumps(meta))


def test_cache_without_owner_is_a_miss(tmp_path):
    _seed(tmp_path, owner_puuid=None)
    calls: list = []
    load_or_fetch("a", fetch=_fetcher("high", calls), cache_dir=tmp_path)
    assert len(calls) == 1


def test_cache_for_other_riot_id_is_a_miss(tmp_path):
    _seed(tmp_path, riot_id="Someone#Else")
    calls: list = []
    load_or_fetch("a", fetch=_fetcher("high", calls), cache_dir=tmp_path)
    assert len(calls) == 1


def test_low_result_is_not_written(tmp_path):
    load_or_fetch("a", fetch=_fetcher("low", []), cache_dir=tmp_path)
    assert not (tmp_path / "meta.json").exists()


def test_sync_only_cache_refetches_and_falls_back(tmp_path):
    _seed(tmp_path, offset_source="sync_only", offset_s=3.0)
    calls: list = []
    tl = load_or_fetch("a", fetch=_fetcher("high", calls), cache_dir=tmp_path)
    assert len(calls) == 1 and tl.offset_s == 12.5  # treated as a miss for offset
    _seed(tmp_path, offset_source="sync_only", offset_s=3.0)
    tl = load_or_fetch("a", fetch=lambda: None, cache_dir=tmp_path)
    assert tl is not None and tl.offset_s == 3.0 and tl.offset_source == "sync_only"


def test_game_end_falls_back_to_game_duration():
    match = _match(end_ms=None)
    match["info"]["gameDuration"] = 1500  # seconds (post-11.20 units)
    tl = parse_timeline(
        match, _timeline(with_game_end=False), owner_puuid="ME", offset_s=0.0, confidence="high"
    )
    assert tl.game_end_s == 1500.0
