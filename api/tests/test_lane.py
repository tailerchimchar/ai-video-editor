import json
import shutil
from dataclasses import asdict
from pathlib import Path

import pytest

from ai_video_editor.grokbot import digest as dg
from ai_video_editor.league import lane as lane_mod
from ai_video_editor.league import riot_timeline as rt
from ai_video_editor.league.lane import LaneMinute, LaneSummary, parse_lane_frames

FIXTURE = Path(__file__).parent / "fixtures" / "panel_round"


def _match(opp_pos="BOTTOM", opp_champ="Caitlyn"):
    def p(pid, team, champ, pos, puuid):
        return {
            "participantId": pid,
            "teamId": team,
            "championName": champ,
            "teamPosition": pos,
            "puuid": puuid,
            "riotIdGameName": "SecretName",
            "summonerName": "SecretSummoner",
        }

    return {
        "info": {
            "participants": [
                p(1, 100, "Jhin", "BOTTOM", "owner"),
                p(2, 100, "Thresh", "UTILITY", "ally"),
                p(6, 200, opp_champ, opp_pos, "opp"),
                p(7, 200, "Diana", "MIDDLE", "other"),
            ]
        }
    }


def _pf(cs, jg, gold, level):
    return {"minionsKilled": cs, "jungleMinionsKilled": jg, "totalGold": gold, "level": level}


def _timeline(events_by_minute=None):
    frames = []
    for m in range(0, 16):
        frames.append(
            {
                "timestamp": m * 60000,
                "participantFrames": {
                    "1": _pf(m * 8, 0, 500 + m * 300, min(m + 1, 18)),
                    "6": _pf(m * 7, 1, 500 + m * 280, min(m + 1, 18)),
                    "7": _pf(1, 0, 1, 1),
                },
                "events": (events_by_minute or {}).get(m, []),
            }
        )
    return {"info": {"frames": frames}}


def test_opponent_is_same_position_enemy():
    s = parse_lane_frames(_match(), _timeline(), owner_puuid="owner", offset_s=0)
    assert s is not None
    assert s.opponent_champion == "Caitlyn"


def test_cs_and_gold_diff_at_10():
    s = parse_lane_frames(_match(), _timeline(), owner_puuid="owner", offset_s=0)
    assert [m.minute for m in s.minutes] == list(range(0, 15))
    m10 = s.minutes[10]
    assert (m10.owner_cs, m10.opp_cs) == (80, 71)
    assert s.cs_diff_at_10 == 9
    assert s.gold_diff_at_10 == 3500 - 3300


def test_first_blood_from_special_kill():
    events = {
        3: [
            {"type": "CHAMPION_KILL", "timestamp": 180000, "killerId": 1, "victimId": 6},
            {
                "type": "CHAMPION_SPECIAL_KILL",
                "timestamp": 180000,
                "killType": "KILL_FIRST_BLOOD",
                "killerId": 1,
            },
        ]
    }
    s = parse_lane_frames(_match(), _timeline(events), owner_puuid="owner", offset_s=12.5)
    assert s.first_blood_s == 192.5
    assert s.owner_first_blood is True
    assert s.first_lane_kill_s == 192.5


def test_first_blood_by_other_and_lane_kill_ignores_other_lanes():
    events = {
        2: [
            {"type": "CHAMPION_KILL", "timestamp": 120000, "killerId": 7, "victimId": 2},
            {
                "type": "CHAMPION_SPECIAL_KILL",
                "timestamp": 120000,
                "killType": "KILL_FIRST_BLOOD",
                "killerId": 7,
            },
        ],
        5: [{"type": "CHAMPION_KILL", "timestamp": 300000, "killerId": 6, "victimId": 1}],
    }
    s = parse_lane_frames(_match(), _timeline(events), owner_puuid="owner", offset_s=0)
    assert s.first_blood_s == 120.0
    assert s.owner_first_blood is False
    assert s.first_lane_kill_s == 300.0


def test_returns_none_without_lane_opponent():
    assert (
        parse_lane_frames(_match(opp_pos=""), _timeline(), owner_puuid="owner", offset_s=0) is None
    )


def test_index_champion_mismatch_raises():
    tl = _timeline()
    tl["info"]["frames"][3]["participantFrames"]["6"] = _pf(1, 0, 1, 1)
    del tl["info"]["frames"][3]["participantFrames"]["1"]
    with pytest.raises(ValueError):
        parse_lane_frames(_match(), tl, owner_puuid="owner", offset_s=0)
    m = _match(opp_champ="")
    with pytest.raises(ValueError):
        parse_lane_frames(m, _timeline(), owner_puuid="owner", offset_s=0)


def test_opponent_named_by_champion_only():
    s = parse_lane_frames(_match(), _timeline(), owner_puuid="owner", offset_s=0)
    blob = json.dumps(asdict(s))
    assert "riotIdGameName" not in blob and "summonerName" not in blob
    assert "SecretName" not in blob and "SecretSummoner" not in blob


def test_lane_summary_path(tmp_path, monkeypatch):
    monkeypatch.setattr(rt.settings, "workspace_dir", str(tmp_path))
    assert lane_mod.lane_summary_path("a1") == tmp_path / "riot" / "a1" / "lane.json"


def test_digest_lane_section(tmp_path):
    dest = tmp_path / "2026-01-01_fixture"
    shutil.copytree(FIXTURE, dest)
    lane = LaneSummary("Caitlyn", [LaneMinute(1, 8, 800, 2, 7, 780, 2)], 1, 20, None, None, False)
    d = dg.build_digest(dest, game="lol", lane=lane)
    out = json.loads(d.model_dump_json())
    assert out["lane"]["opponent_champion"] == "Caitlyn"
    assert out["lane"]["minutes"][0]["owner_cs"] == 8
    assert dg.build_digest(dest, game="lol").lane is None
