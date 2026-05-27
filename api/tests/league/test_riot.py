"""Riot correlation — the part we burned the most time on. These cover
the exact bugs that bit us: wrong-game pick, ambiguous filename anchor,
game-clock vs wall-clock mapping, confidence boundaries."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from ai_video_editor.league.candidates.riot import (
    _confidence,
    _correlate,
    _event_candidates,
    _expected_start_ms,
    _kill_candidates,
    _our_participant,
)


def _m(start_ms: int, end_ms: int, participants: list[dict], mid: str = "NA1_X") -> dict:
    return {
        "metadata": {"matchId": mid},
        "info": {
            "gameStartTimestamp": start_ms,
            "gameEndTimestamp": end_ms,
            "participants": participants,
        },
    }


def test_expected_start_ms_parses_filename_in_central_tz():
    # CDT (May) is UTC-5 → local 21:19:08 → 02:19:08 UTC next day.
    fn = "League of Legends_05-15-2026_21-19-8-0.mp4"
    ms = _expected_start_ms(fn)
    assert ms is not None
    got = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    assert got == datetime(2026, 5, 16, 2, 19, 8, tzinfo=ZoneInfo("UTC"))


def test_expected_start_ms_unparseable_returns_none():
    assert _expected_start_ms("not-an-outplayed-name.mp4") is None


def test_correlate_picks_match_via_finalize_anchored_when_better():
    # Filename time fn_ms = 100s. Recording dur = 90s. So the FINALIZE
    # interpretation puts real start at 10s. A match starting near 10s
    # should win even though its start is "far" from the filename time.
    fn_ms, dur_ms = 100_000, 90_000
    m_start = _m(50_000, 140_000, [{"puuid": "ME", "participantId": 1}], mid="START")
    m_end = _m(11_000, 101_000, [{"puuid": "ME", "participantId": 1}], mid="END")
    chosen, ds, _ = _correlate(fn_ms, dur_ms, [m_start, m_end])
    assert chosen["metadata"]["matchId"] == "END"
    # End-anchored: anchor 10s vs game 11s → 1s delta (tiny).
    assert ds <= 5_000


def test_correlate_empty_matches_returns_none():
    chosen, _, _ = _correlate(0, 0, [])
    assert chosen is None


def test_confidence_thresholds():
    assert _confidence(0, 0) == "high"
    assert _confidence(60_000, 0) == "high"  # 60s start, 0s dur → still HIGH
    assert _confidence(60_000, 200_000) == "medium"  # dur > 180s → demote to medium
    assert _confidence(400_000, 0) == "medium"  # ds in (300, 900]
    assert _confidence(1_200_000, 0) == "low"  # 20min off


def test_our_participant_returns_dict_or_none():
    info = _m(0, 0, [{"puuid": "ME", "championName": "Jhin", "participantId": 5}])
    assert _our_participant(info, "ME")["championName"] == "Jhin"
    assert _our_participant(info, "OTHER") is None


def test_kill_candidates_maps_game_clock_to_offsets_no_wallclock_math():
    # The bug we squashed: kills used to be mapped via wall-clock
    # arithmetic against ctime; now offset == game-clock time directly
    # (because a full recording spans the game).
    me = {"puuid": "ME", "participantId": 5, "championName": "Jhin"}
    match = _m(0, 1_800_000, [me])

    def ev(ts: int, killer: int, victim: int) -> dict:
        return {"type": "CHAMPION_KILL", "timestamp": ts, "killerId": killer, "victimId": victim}

    timeline = {
        "info": {
            "frames": [
                {
                    "events": [
                        ev(60_000, 5, 9),  # our kill at game-clock 60s
                        ev(120_000, 9, 5),  # our death at 120s
                        ev(200_000, 5, 9),  # past duration → filtered
                    ]
                }
            ]
        }
    }
    out = _kill_candidates(match, timeline, "ME", duration_s=150.0, pad=4.0)
    assert len(out) == 2
    assert out[0]["event_type"] == "kill"
    # offset = ev_ts/1000 (= 60s); window ±pad → 56..64
    assert out[0]["metadata"]["anchor_seconds"] == 60.0
    assert out[0]["start_seconds"] == 56.0
    assert out[0]["end_seconds"] == 64.0
    assert out[1]["event_type"] == "death"
    # Champion captured for folder naming.
    assert out[0]["metadata"]["champion"] == "Jhin"


def test_kill_candidates_attaches_correlation_diagnostic():
    me = {"puuid": "ME", "participantId": 5, "championName": "Jhin"}
    match = _m(0, 100_000, [me])
    kill = {"type": "CHAMPION_KILL", "timestamp": 30_000, "killerId": 5, "victimId": 9}
    timeline = {"info": {"frames": [{"events": [kill]}]}}
    corr = {"confidence": "high", "delta_start_s": 1.0, "delta_dur_s": 0.5}
    out = _kill_candidates(match, timeline, "ME", duration_s=50.0, pad=2.0, corr=corr)
    md = out[0]["metadata"]
    assert md["correlation_confidence"] == "high"
    assert md["delta_start_seconds"] == 1.0


# ----- L1: expanded event types -----


def _me():
    return {"puuid": "ME", "participantId": 5, "championName": "Jhin"}


def _tl(events: list[dict]) -> dict:
    return {"info": {"frames": [{"events": events}]}}


def _kill(ts: int, killer: int = 5, victim: int = 9) -> dict:
    return {"type": "CHAMPION_KILL", "timestamp": ts, "killerId": killer, "victimId": victim}


def _special(ts: int, killer: int, kill_type: str, multi: int | None = None) -> dict:
    ev = {
        "type": "CHAMPION_SPECIAL_KILL",
        "timestamp": ts,
        "killerId": killer,
        "killType": kill_type,
    }
    if multi is not None:
        ev["multiKillLength"] = multi
    return ev


def _objective(ts: int, killer: int, monster: str, sub: str | None = None) -> dict:
    ev = {"type": "ELITE_MONSTER_KILL", "timestamp": ts, "killerId": killer, "monsterType": monster}
    if sub:
        ev["monsterSubType"] = sub
    return ev


def test_first_blood_upgrades_matching_kill_event_type():
    match = _m(0, 600_000, [_me()])
    timeline = _tl(
        [
            _kill(60_000),  # our kill at 60s
            _special(60_500, 5, "KILL_FIRST_BLOOD"),  # marker fires 0.5s later
        ]
    )
    out = _event_candidates(match, timeline, "ME", duration_s=600.0, pad=4.0)
    assert len(out) == 1, "marker must not emit a duplicate candidate"
    assert out[0]["event_type"] == "first_blood"
    assert "CHAMPION_SPECIAL_KILL" in out[0]["metadata"]["rationale"]


def test_multikill_upgrades_to_named_tag():
    cases = [(2, "double_kill"), (3, "triple_kill"), (4, "quadra_kill"), (5, "penta_kill")]
    for length, expected_tag in cases:
        timeline = _tl(
            [
                _kill(100_000),
                _special(101_000, 5, "KILL_MULTI", multi=length),
            ]
        )
        out = _event_candidates(_m(0, 600_000, [_me()]), timeline, "ME", 600.0, 4.0)
        assert len(out) == 1
        assert out[0]["event_type"] == expected_tag, f"multi={length}"


def test_ace_upgrade_when_my_kill_triggered_it():
    timeline = _tl([_kill(200_000), _special(200_200, 5, "KILL_ACE")])
    out = _event_candidates(_m(0, 600_000, [_me()]), timeline, "ME", 600.0, 4.0)
    assert len(out) == 1
    assert out[0]["event_type"] == "ace"


def test_special_kill_by_enemy_is_ignored():
    # Enemy got first blood — our underlying kill (if any) must NOT be tagged.
    timeline = _tl(
        [
            _kill(50_000),  # my kill, irrelevant to enemy's FB
            _special(60_000, killer=9, kill_type="KILL_FIRST_BLOOD"),
        ]
    )
    out = _event_candidates(_m(0, 600_000, [_me()]), timeline, "ME", 600.0, 4.0)
    assert len(out) == 1
    assert out[0]["event_type"] == "kill"  # stays a plain kill


def test_multikill_marker_too_far_does_not_upgrade():
    # Marker fires 7s after the kill — outside our 5s match window.
    timeline = _tl(
        [
            _kill(100_000),
            _special(107_500, 5, "KILL_MULTI", multi=3),
        ]
    )
    out = _event_candidates(_m(0, 600_000, [_me()]), timeline, "ME", 600.0, 4.0)
    assert len(out) == 1
    assert out[0]["event_type"] == "kill"


def test_objective_kills_emit_as_separate_candidates():
    timeline = _tl(
        [
            _objective(300_000, 5, "BARON_NASHOR"),
            _objective(400_000, 5, "DRAGON", sub="INFERNAL_DRAGON"),
            _objective(500_000, 5, "RIFTHERALD"),
            _objective(550_000, killer=9, monster="BARON_NASHOR"),  # enemy took it — skip
        ]
    )
    out = _event_candidates(_m(0, 600_000, [_me()]), timeline, "ME", 600.0, 4.0)
    tags = [c["event_type"] for c in out]
    assert tags == ["objective_baron", "objective_dragon", "objective_herald"]
    # Dragon row carries subtype for downstream "you took infernal" labeling.
    dragon = next(c for c in out if c["event_type"] == "objective_dragon")
    assert dragon["metadata"]["monster_subtype"] == "INFERNAL_DRAGON"


def test_unknown_monster_type_is_skipped():
    timeline = _tl([_objective(300_000, 5, "HORDE")])  # voidgrubs etc. — not mapped
    out = _event_candidates(_m(0, 600_000, [_me()]), timeline, "ME", 600.0, 4.0)
    assert out == []


def test_kills_and_objectives_returned_in_chronological_order():
    timeline = _tl(
        [
            _objective(450_000, 5, "DRAGON"),
            _kill(100_000),
            _objective(50_000, 5, "RIFTHERALD"),
            _kill(200_000),
        ]
    )
    out = _event_candidates(_m(0, 600_000, [_me()]), timeline, "ME", 600.0, 4.0)
    assert [c["event_type"] for c in out] == [
        "objective_herald",
        "kill",
        "kill",
        "objective_dragon",
    ]


def test_objective_outside_recording_is_filtered():
    # ELITE_MONSTER_KILL at game-clock 700s but recording is only 600s long.
    timeline = _tl([_objective(700_000, 5, "BARON_NASHOR")])
    out = _event_candidates(_m(0, 1_000_000, [_me()]), timeline, "ME", 600.0, 4.0)
    assert out == []
