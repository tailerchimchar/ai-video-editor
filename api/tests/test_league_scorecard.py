import json

from ai_video_editor.league import riot_timeline as rt
from ai_video_editor.league import scorecard as sc
from ai_video_editor.league.riot_timeline import RiotTimeline, TimelineEvent

MATCH = {
    "metadata": {"matchId": "NA1_42"},
    "info": {
        "gameDuration": 1925,
        "participants": [
            {
                "participantId": 1,
                "puuid": "me",
                "teamId": 100,
                "championName": "Zeri",
                "kills": 8,
                "deaths": 2,
                "assists": 13,
                "win": True,
            },
            {"participantId": 6, "puuid": "x", "teamId": 200, "championName": "Diana"},
        ],
    },
}


def _ev(kind, t):
    return TimelineEvent(kind, t, t + 5.0, ["you (Zeri)"], {})


def _timeline(events):
    return RiotTimeline(
        match_id="NA1_42",
        confidence="high",
        offset_s=5.0,
        game_end_s=1930.0,
        events=events,
        owner_frames=[],
    )


def test_three_enemy_deaths_with_owner_kill_is_a_multikill_fight():
    tl = _timeline(
        [_ev("enemy_death", 100), _ev("kill", 108), _ev("assist", 114), _ev("death", 500)]
    )
    score = sc.game_scorecard(tl, MATCH)
    assert score.multikill_fights == [
        {"start_s": 105.0, "end_s": 119.0, "enemy_deaths": 3, "owner_kills": 1, "owner_assists": 1}
    ]
    assert score.aces == 0


def test_five_enemy_deaths_in_one_fight_is_an_ace():
    tl = _timeline([_ev("enemy_death", 100 + i * 3) for i in range(5)])
    assert sc.game_scorecard(tl, MATCH).aces == 1


def test_two_owner_kills_within_10s_is_an_owner_multikill():
    tl = _timeline([_ev("kill", 200), _ev("kill", 208), _ev("kill", 300)])
    assert sc.game_scorecard(tl, MATCH).owner_multikills == [{"t_s": 205.0, "count": 2}]


def test_kda_result_champion_from_match():
    score = sc.game_scorecard(_timeline([]), MATCH, owner_puuid="me")
    assert (score.kda, score.result, score.champion) == ("8/2/13", "WIN", "Zeri")
    assert score.match_id == "NA1_42"
    assert score.duration_s == 1925


def test_cli_prints_one_line_per_cached_game(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(rt.settings, "workspace_dir", tmp_path)
    monkeypatch.setattr(rt.settings, "riot_id", "me#NA1")
    d = tmp_path / "riot" / "asset1"
    d.mkdir(parents=True)
    (d / "match.json").write_text(json.dumps(MATCH), encoding="utf-8")
    (d / "timeline.json").write_text(
        json.dumps(
            {
                "info": {
                    "frames": [
                        {
                            "events": [
                                {
                                    "type": "CHAMPION_KILL",
                                    "timestamp": t * 1000,
                                    "killerId": 1,
                                    "victimId": 6,
                                    "assistingParticipantIds": [],
                                }
                                for t in (100, 105, 110)
                            ]
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    (d / "meta.json").write_text(
        json.dumps(
            {
                "match_id": "NA1_42",
                "confidence": "high",
                "offset_s": 0.0,
                "owner_puuid": "me",
                "riot_id": "me#NA1",
            }
        ),
        encoding="utf-8",
    )
    assert sc.main([]) == 0
    line = capsys.readouterr().out.strip()
    assert line.split()[:5] == ["NA1_42", "Zeri", "8/2/13", "WIN", "32:05"]
    assert sc.main(["--asset", "nope"]) == 0
