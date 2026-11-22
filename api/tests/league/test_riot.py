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


# ----- Jhin round T1: Retry-After, fight metadata, cache wiring -----

import httpx  # noqa: E402
import pytest  # noqa: E402

from ai_video_editor.league.candidates import riot as riot_mod  # noqa: E402


@pytest.fixture
def riot_cfg(monkeypatch, tmp_path):
    from ai_video_editor.config import settings

    monkeypatch.setattr(settings, "riot_api_key", "k")
    monkeypatch.setattr(settings, "riot_id", "Noodle#NA1")
    monkeypatch.setattr(settings, "riot_sync_offset_seconds", 0.0)
    monkeypatch.setattr(settings, "recording_timezone", "UTC")
    monkeypatch.setattr(settings, "workspace_dir", tmp_path)
    sleeps: list[float] = []
    monkeypatch.setattr(riot_mod, "_sleep", sleeps.append)
    return sleeps


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_get_honours_retry_after_once(riot_cfg):
    calls = []

    def handler(req):
        calls.append(req)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "3"})
        return httpx.Response(200, json={"ok": True})

    with _client(handler) as c:
        assert riot_mod._get(c, "/x") == {"ok": True}
    assert riot_cfg == [3.0]
    assert len(calls) == 2


def test_get_second_429_raises_and_caps_sleep(riot_cfg):
    def handler(req):
        return httpx.Response(429, headers={"Retry-After": "60"})

    with _client(handler) as c, pytest.raises(httpx.HTTPStatusError):
        riot_mod._get(c, "/x")
    assert riot_cfg == [10.0]


def test_get_5xx_raises_without_retry(riot_cfg):
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(503)

    with _client(handler) as c, pytest.raises(httpx.HTTPStatusError):
        riot_mod._get(c, "/x")
    assert len(calls) == 1 and riot_cfg == []


# Recording: filename local (UTC) 2026-11-20 20:00:00, 1000 s long.
_START_MS = int(datetime(2026, 11, 20, 20, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
_ASSET = {
    "id": "asset-jhin",
    "game": "League of Legends",
    "filename": "League of Legends_11-20-2026_20-0-0-000.mp4",
}


def _jhin_match() -> dict:
    parts = [
        {"puuid": "ME", "participantId": 1, "championName": "Jhin", "teamId": 100},
        {"puuid": "A2", "participantId": 2, "championName": "Thresh", "teamId": 100},
        {"puuid": "E6", "participantId": 6, "championName": "Diana", "teamId": 200},
    ]
    return _m(_START_MS, _START_MS + 990_000, parts, mid="NA1_JHIN")


def _jhin_timeline() -> dict:
    return _tl(
        [
            {"type": "CHAMPION_KILL", "timestamp": 300_000, "killerId": 6, "victimId": 2},
            {
                "type": "CHAMPION_KILL",
                "timestamp": 310_000,
                "killerId": 1,
                "victimId": 6,
                "assistingParticipantIds": [2],
            },
            {"type": "CHAMPION_KILL", "timestamp": 600_000, "killerId": 6, "victimId": 1},
            {"type": "GAME_END", "timestamp": 990_000, "winningTeam": 100},
        ]
    )


def _riot_handler(calls: list, match: dict | None = None):
    match = match or _jhin_match()

    def handler(req):
        calls.append(req.url.path)
        p = req.url.path
        if "by-riot-id" in p:
            return httpx.Response(200, json={"puuid": "ME"})
        if p.endswith("/ids"):
            return httpx.Response(200, json=["NA1_JHIN"])
        if p.endswith("/timeline"):
            return httpx.Response(200, json=_jhin_timeline())
        return httpx.Response(200, json=match)

    return handler


def test_detect_attaches_fight_metadata_and_caches(riot_cfg, monkeypatch, tmp_path):
    calls: list = []
    monkeypatch.setattr(riot_mod, "_client_factory", lambda: _client(_riot_handler(calls)))
    rows, status = riot_mod.detect_riot_events(_ASSET, 1000.0)
    assert status == "ok"
    kill = next(r for r in rows if r["event_type"] == "kill")
    md = kill["metadata"]
    assert md["fight_start_s"] == 300.0
    assert md["fight_end_s"] == 310.0
    assert md["game_end_s"] == 990.0
    assert md["match_confidence"] == "high"
    death = next(r for r in rows if r["event_type"] == "death")
    assert death["metadata"]["fight_start_s"] == 600.0
    assert (tmp_path / "riot" / "asset-jhin" / "meta.json").exists()

    # Second run: served from cache, no lookback calls.
    calls.clear()
    rows2, status2 = riot_mod.detect_riot_events(_ASSET, 1000.0)
    assert status2 == "ok" and calls == []
    assert [r["metadata"]["fight_start_s"] for r in rows2] == [
        r["metadata"]["fight_start_s"] for r in rows
    ]


def test_detect_low_confidence_has_no_fight_metadata(riot_cfg, monkeypatch):
    far = _jhin_match()
    far["info"]["gameStartTimestamp"] += 3_600_000  # an hour off -> low
    far["info"]["gameEndTimestamp"] += 3_600_000
    calls: list = []
    monkeypatch.setattr(riot_mod, "_client_factory", lambda: _client(_riot_handler(calls, far)))
    rows, _ = riot_mod.detect_riot_events(_ASSET, 1000.0)
    assert rows, "low-confidence candidates are still surfaced as before"
    for r in rows:
        assert "fight_start_s" not in r["metadata"]
        assert "match_confidence" not in r["metadata"]
    # a low pick is never written to the cache
    from ai_video_editor.config import settings

    assert not (settings.workspace_dir / "riot" / "asset-jhin").exists()


def test_detect_rate_limited_twice_returns_empty(riot_cfg, monkeypatch):
    monkeypatch.setattr(
        riot_mod, "_client_factory", lambda: _client(lambda req: httpx.Response(429))
    )
    assert riot_mod.detect_riot_events(_ASSET, 1000.0) == ([], "rate_limited")


def test_fetch_timeline_inputs_feeds_load_or_fetch(riot_cfg, monkeypatch, tmp_path):
    from ai_video_editor.league.riot_timeline import load_or_fetch

    monkeypatch.setattr(riot_mod, "_client_factory", lambda: _client(_riot_handler([])))
    tl = load_or_fetch(
        "asset-jhin",
        fetch=lambda: riot_mod.fetch_timeline_inputs(_ASSET, 1000.0),
        cache_dir=tmp_path / "c",
    )
    assert tl is not None and tl.match_id == "NA1_JHIN" and tl.confidence == "high"


# ----- Fix round 1: one offset basis for clips and timeline -----

_SYNC = 3.0
_CALIBRATED = 20.0


@pytest.fixture
def offset_cfg(riot_cfg, monkeypatch):
    from ai_video_editor.config import settings

    monkeypatch.setattr(settings, "riot_sync_offset_seconds", _SYNC)
    monkeypatch.setattr(settings, "riot_offset_override_seconds", None)
    monkeypatch.setattr(settings, "riot_offset_min_quality", 0.0)
    return riot_cfg


def test_end_to_end_fight_bounds_on_calibrated_plus_sync_basis(offset_cfg, monkeypatch, tmp_path):
    import json

    import numpy as np

    from ai_video_editor.league import orchestrator as orch

    monkeypatch.setattr(riot_mod, "_client_factory", lambda: _client(_riot_handler([])))
    monkeypatch.setattr(orch, "align_offset", lambda kills, energy: (_CALIBRATED, 5.0))
    asset = {**_ASSET, "path": str(tmp_path / "rec.mp4")}
    rows, status = orch.gather_lol_candidates(asset, 1000.0, np.zeros(1000))
    assert status == "ok"
    basis = _CALIBRATED + _SYNC
    kill = next(r for r in rows if r["event_type"] == "kill")["metadata"]
    assert kill["anchor_seconds"] == 310.0 + basis
    assert kill["fight_start_s"] == 300.0 + basis  # earliest fight event (ally death)
    assert kill["fight_end_s"] == 310.0 + basis
    assert kill["game_end_s"] == 990.0 + basis
    meta = json.loads((tmp_path / "riot" / "asset-jhin" / "meta.json").read_text())
    assert meta["offset_s"] == basis
    assert meta["offset_source"] == "audio"  # orchestrator overwrote the sync_only write


def test_fetch_timeline_inputs_returns_calibrated_offset(offset_cfg, monkeypatch, tmp_path):
    import json

    import numpy as np

    from ai_video_editor.league import offset as offset_mod
    from ai_video_editor.league.riot_timeline import load_or_fetch

    rec = tmp_path / "rec.mp4"
    rec.write_bytes(b"x")
    monkeypatch.setattr(riot_mod, "_client_factory", lambda: _client(_riot_handler([])))
    monkeypatch.setattr(offset_mod, "align_offset", lambda kills, energy: (_CALIBRATED, 5.0))
    asset = {**_ASSET, "path": str(rec)}
    cdir = tmp_path / "c"
    tl = load_or_fetch(
        "asset-jhin",
        fetch=lambda: riot_mod.fetch_timeline_inputs(
            asset, 1000.0, energy_fn=lambda p, d: np.zeros(1000)
        ),
        cache_dir=cdir,
    )
    basis = _CALIBRATED + _SYNC
    assert tl is not None and tl.offset_s == basis and tl.offset_source == "audio"
    kill = next(e for e in tl.events if e.kind == "kill")
    assert kill.t_rec_s == 310.0 + basis  # same second as the clip anchor
    meta = json.loads((cdir / "meta.json").read_text())
    assert meta["offset_s"] == basis and meta["offset_source"] == "audio"


def test_fetch_timeline_inputs_without_recording_is_sync_only(offset_cfg, monkeypatch, tmp_path):
    import json

    from ai_video_editor.league.riot_timeline import load_or_fetch

    monkeypatch.setattr(riot_mod, "_client_factory", lambda: _client(_riot_handler([])))
    asset = {**_ASSET, "path": str(tmp_path / "gone.mp4")}
    cdir = tmp_path / "c"
    tl = load_or_fetch(
        "asset-jhin", fetch=lambda: riot_mod.fetch_timeline_inputs(asset, 1000.0), cache_dir=cdir
    )
    assert tl is not None and tl.offset_s == _SYNC and tl.offset_source == "sync_only"
    assert json.loads((cdir / "meta.json").read_text())["offset_source"] == "sync_only"


@pytest.mark.parametrize("energy_fn", ["raises", "none"])
def test_failed_audio_decode_is_sync_only_not_audio(offset_cfg, monkeypatch, tmp_path, energy_fn):
    import json

    from ai_video_editor.league import offset as offset_mod
    from ai_video_editor.league.riot_timeline import load_or_fetch

    def boom(p, d):
        raise RuntimeError("ffmpeg failed")

    fn = boom if energy_fn == "raises" else (lambda p, d: None)
    rec = tmp_path / "rec.mp4"
    rec.write_bytes(b"x")
    monkeypatch.setattr(riot_mod, "_client_factory", lambda: _client(_riot_handler([])))
    monkeypatch.setattr(
        offset_mod, "verify_offset", lambda *a: pytest.fail("no calibration without audio")
    )
    asset = {**_ASSET, "path": str(rec)}
    cdir = tmp_path / "c"
    tl = load_or_fetch(
        "asset-jhin",
        fetch=lambda: riot_mod.fetch_timeline_inputs(asset, 1000.0, energy_fn=fn),
        cache_dir=cdir,
    )
    assert tl is not None and tl.offset_s == _SYNC and tl.offset_source == "sync_only"
    assert json.loads((cdir / "meta.json").read_text())["offset_source"] == "sync_only"


# ----- I6b: lane.json is written after a successful correlation -----


def test_detect_writes_lane_json_after_caching(riot_cfg, monkeypatch, tmp_path):
    from ai_video_editor.league.lane import LaneSummary, lane_summary_path, load_lane_summary

    seen = {}

    def fake_parse(match, timeline, *, owner_puuid, offset_s):
        seen.update(owner=owner_puuid, offset=offset_s)
        return LaneSummary("Diana", cs_diff_at_10=7)

    monkeypatch.setattr(riot_mod, "parse_lane_frames", fake_parse)
    calls: list = []
    monkeypatch.setattr(riot_mod, "_client_factory", lambda: _client(_riot_handler(calls)))
    _, status = riot_mod.detect_riot_events(_ASSET, 1000.0)
    assert status == "ok"
    assert seen == {"owner": "ME", "offset": 0.0}
    written = load_lane_summary(lane_summary_path("asset-jhin"))
    assert written == LaneSummary("Diana", cs_diff_at_10=7)


def test_lane_parse_failure_never_breaks_candidates(riot_cfg, monkeypatch, tmp_path):
    from ai_video_editor.league.lane import lane_summary_path

    def boom(*a, **k):
        raise ValueError("participant index mismatch")

    monkeypatch.setattr(riot_mod, "parse_lane_frames", boom)
    monkeypatch.setattr(riot_mod, "_client_factory", lambda: _client(_riot_handler([])))
    rows, status = riot_mod.detect_riot_events(_ASSET, 1000.0)
    assert status == "ok" and rows
    assert not lane_summary_path("asset-jhin").exists()
