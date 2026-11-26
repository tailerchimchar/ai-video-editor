"""AVE-54: early-game segments from the cached Riot timeline (synthetic + one real-cache smoke)."""

from __future__ import annotations

import itertools
import json
from pathlib import Path

import pytest

from ai_video_editor.league.early_game import EarlySegment, early_segments, first_back_s
from ai_video_editor.league.riot_timeline import parse_timeline

OWNER = 4
ENEMY = 9
OFFSET = 100.0
BASE = {"x": 500, "y": 500}
LANE = {"x": 12500, "y": 1500}  # bot_lane
BUSH = {"x": 11800, "y": 3200}  # bot tri-bush / river bush region
JUNGLE = {"x": 8000, "y": 3500}  # blue bot jungle


def match() -> dict:
    parts = [
        {
            "participantId": i,
            "puuid": "me" if i == OWNER else f"p{i}",
            "championName": "Zeri" if i == OWNER else f"C{i}",
            "teamId": 100 if i <= 5 else 200,
        }
        for i in range(1, 11)
    ]
    return {"metadata": {"matchId": "T_1"}, "info": {"participants": parts}}


def frame(minute: int, events=(), pos=None, hp=(500, 500), enemy_pos=None) -> dict:
    pfs = {}
    for i in range(1, 11):
        p = {"position": dict(BASE), "championStats": {"health": 500, "healthMax": 500}}
        if i == OWNER:
            if pos:
                p["position"] = pos
            p["championStats"] = {"health": hp[0], "healthMax": hp[1]}
        if i == ENEMY and enemy_pos:
            p["position"] = enemy_pos
        pfs[str(i)] = p
    return {"timestamp": minute * 60000, "participantFrames": pfs, "events": list(events)}


def buy(t_s: float, item=1055, pid=OWNER) -> dict:
    return {
        "type": "ITEM_PURCHASED",
        "timestamp": int(t_s * 1000),
        "participantId": pid,
        "itemId": item,
    }


def lvl(t_s: float, level: int, pid=OWNER) -> dict:
    return {"type": "LEVEL_UP", "timestamp": int(t_s * 1000), "participantId": pid, "level": level}


def kill(t_s: float, killer=OWNER, victim=ENEMY, assists=()) -> dict:
    return {
        "type": "CHAMPION_KILL",
        "timestamp": int(t_s * 1000),
        "killerId": killer,
        "victimId": victim,
        "assistingParticipantIds": list(assists),
    }


def raw(frames: list[dict]) -> dict:
    return {"info": {"frames": frames}}


def build(frames: list[dict]):
    r = raw(frames)
    return parse_timeline(match(), r, owner_puuid="me", offset_s=OFFSET, confidence="high"), r


def quiet(n: int = 8) -> list[dict]:
    return [frame(m, pos=LANE) for m in range(n)]


def kinds(segs) -> list[str]:
    return [s.kind for s in segs]


def of_kind(segs, kind):
    return [s for s in segs if s.kind == kind]


# --- step 1: model + first_back_s ---


def test_model_defaults():
    s = EarlySegment(kind="opening", start_s=1.0, end_s=2.0, reason="x")
    assert s.confirmed is False


def test_first_back_skips_starting_items_and_converts():
    frames = quiet()
    frames[0]["events"] = [buy(1, 1055), buy(2, 2003)]
    frames[3]["events"] = [buy(200.0, 3070)]
    assert first_back_s(raw(frames), OWNER, OFFSET) == pytest.approx(300.0)


def test_first_back_ignores_other_players_and_none_when_absent():
    frames = quiet()
    frames[3]["events"] = [buy(200.0, pid=ENEMY)]
    assert first_back_s(raw(frames), OWNER, OFFSET) is None


# --- step 2: opening ---


def test_no_early_events_is_opening_plus_first_back_only():
    frames = quiet()
    frames[3]["events"] = [buy(200.0, 3070)]
    tl, r = build(frames)
    segs = early_segments(tl, r)
    assert kinds(segs) == ["opening", "first_back"]
    op, fb = segs
    assert (op.start_s, op.end_s) == (160.0, 220.0)  # 1:00-2:00 + offset
    assert (fb.start_s, fb.end_s) == (288.0, 308.0)  # 200-12 .. 200+8 + offset
    assert fb.confirmed is False


def test_opening_present_without_any_purchase():
    tl, r = build(quiet())
    assert kinds(early_segments(tl, r)) == ["opening"]


def test_opening_starts_at_0_20_when_kill_before_1_05():
    # Owner 2026-11-26: 0:50 missed a fight that began around 0:30.
    frames = quiet()
    frames[0]["events"] = [kill(58.0, killer=ENEMY, victim=7)]
    tl, r = build(frames)
    assert early_segments(tl, r)[0].start_s == 120.0  # game 0:20 + offset 100


def test_opening_starts_at_0_20_when_both_laners_in_bush():
    frames = quiet()
    frames[1] = frame(1, pos=BUSH, enemy_pos=BUSH)
    tl, r = build(frames)
    assert early_segments(tl, r)[0].start_s == 120.0  # game 0:20 + offset 100


def test_opening_one_laner_in_bush_is_not_enough():
    frames = quiet()
    frames[1] = frame(1, pos=BUSH)
    tl, r = build(frames)
    assert early_segments(tl, r)[0].start_s == 160.0


def test_opening_kill_after_1_05_does_not_pull_start_earlier():
    frames = quiet()
    frames[1]["events"] = [kill(70.0)]
    tl, r = build(frames)
    assert early_segments(tl, r)[0].start_s == 160.0


def test_opening_end_extends_to_level2_plus_10_capped_at_2_30():
    frames = quiet()
    frames[2]["events"] = [lvl(125.0, 2)]
    tl, r = build(frames)
    assert early_segments(tl, r)[0].end_s == 235.0  # 135 + 100
    frames[2]["events"] = [lvl(200.0, 2)]
    tl, r = build(frames)
    assert early_segments(tl, r)[0].end_s == 250.0  # capped at 150 + 100
    frames[2]["events"] = [lvl(60.0, 2)]
    tl, r = build(frames)
    assert early_segments(tl, r)[0].end_s == 220.0  # never before 2:00


# --- step 3: level3_trade ---


def test_level3_trade_with_kill_within_20s():
    frames = quiet()
    frames[3]["events"] = [lvl(190.0, 3), kill(200.0)]
    tl, r = build(frames)
    segs = early_segments(tl, r)
    assert kinds(segs) == ["opening", "level3_trade"]
    t = segs[1]
    assert t.start_s <= 190.0 + OFFSET and t.end_s >= 200.0 + OFFSET


def test_level3_trade_via_assist_or_owner_death():
    for ev in (
        kill(195.0, killer=2, victim=ENEMY, assists=[OWNER]),
        kill(195.0, killer=ENEMY, victim=OWNER),
    ):
        frames = quiet()
        frames[3]["events"] = [lvl(190.0, 3), ev]
        tl, r = build(frames)
        assert "level3_trade" in kinds(early_segments(tl, r))


def test_level3_trade_via_low_hp_next_frame():
    frames = quiet()
    frames[3]["events"] = [lvl(190.0, 3)]
    frames[4] = frame(4, pos=LANE, hp=(240, 500))
    tl, r = build(frames)
    assert "level3_trade" in kinds(early_segments(tl, r))


def test_level3_without_trade_is_not_a_segment():
    frames = quiet()
    frames[3]["events"] = [lvl(190.0, 3), kill(260.0)]  # 70 s later, hp full
    tl, r = build(frames)
    assert "level3_trade" not in kinds(early_segments(tl, r))


def test_level3_trade_ignores_other_players_level_up():
    frames = quiet()
    frames[3]["events"] = [lvl(190.0, 3, pid=ENEMY), kill(195.0)]
    tl, r = build(frames)
    assert "level3_trade" not in kinds(early_segments(tl, r))


# --- step 4: jungle_move / low_hp ---


def test_jungle_move_before_first_back():
    frames = quiet(8)
    frames[3] = frame(3, pos=JUNGLE)
    frames[5]["events"] = [buy(310.0)]
    tl, r = build(frames)
    segs = early_segments(tl, r)
    j = of_kind(segs, "jungle_move")
    assert len(j) == 1
    assert j[0].start_s <= 180.0 + OFFSET <= j[0].end_s


def test_jungle_after_first_back_is_ignored():
    frames = quiet(8)
    frames[2]["events"] = [buy(160.0)]
    frames[5] = frame(5, pos=JUNGLE)
    tl, r = build(frames)
    assert "jungle_move" not in kinds(early_segments(tl, r))


def test_low_hp_before_first_back():
    frames = quiet(8)
    frames[4] = frame(4, pos=LANE, hp=(100, 500))
    frames[6]["events"] = [buy(400.0)]
    tl, r = build(frames)
    lo = of_kind(early_segments(tl, r), "low_hp")
    assert len(lo) == 1 and lo[0].start_s <= 240.0 + OFFSET <= lo[0].end_s


def test_hp_above_quarter_is_not_low():
    frames = quiet(8)
    frames[4] = frame(4, pos=LANE, hp=(130, 500))
    tl, r = build(frames)
    assert "low_hp" not in kinds(early_segments(tl, r))


# --- step 5: give_recall ---


def test_give_recall_purchase_before_6_00_that_is_not_first_back():
    frames = quiet(10)
    frames[3]["events"] = [buy(200.0, 3070)]
    frames[5]["events"] = [buy(330.0, 1056)]
    tl, r = build(frames)
    segs = early_segments(tl, r)
    assert kinds(segs) == ["opening", "first_back", "give_recall"]
    assert segs[2].start_s == 330.0 - 12 + OFFSET


def test_purchase_after_6_00_or_same_trip_is_not_give_recall():
    frames = quiet(10)
    frames[3]["events"] = [buy(200.0, 3070), buy(205.0, 1056), buy(400.0, 1057)]
    tl, r = build(frames)
    assert "give_recall" not in kinds(early_segments(tl, r))


def test_purchase_right_after_owner_death_is_not_give_recall():
    frames = quiet(10)
    frames[3]["events"] = [buy(200.0, 3070)]
    frames[5]["events"] = [kill(315.0, killer=ENEMY, victim=OWNER), buy(330.0, 1056)]
    tl, r = build(frames)
    assert "give_recall" not in kinds(early_segments(tl, r))


# --- step 6: merge / sort / cap ---


def test_overlapping_segments_merge_and_priority_kind_wins():
    frames = quiet(8)
    frames[3]["events"] = [buy(200.0, 3070)]
    frames[3]["participantFrames"]["4"]["position"] = (
        JUNGLE  # frame 3 = 180 s, within first-back window
    )
    tl, r = build(frames)
    segs = early_segments(tl, r)
    assert kinds(segs) == ["opening", "first_back"]
    assert "jungle" in segs[1].reason and "first back" in segs[1].reason


def test_sorted_nonoverlapping_and_capped_at_first_back_plus_8():
    frames = quiet(10)
    frames[3]["events"] = [buy(200.0, 3070)]
    frames[3]["participantFrames"]["4"]["position"] = JUNGLE
    frames[4] = frame(4, pos=JUNGLE, hp=(50, 500))  # frame at 240 s, after back
    frames[5]["events"] = [lvl(300.0, 3), kill(305.0), buy(330.0, 1056)]
    tl, r = build(frames)
    segs = early_segments(tl, r)
    cap = 200.0 + 8 + OFFSET
    assert [s.start_s for s in segs] == sorted(s.start_s for s in segs)
    assert all(a.end_s < b.start_s for a, b in itertools.pairwise(segs))
    assert all(s.end_s <= cap or s.kind == "give_recall" for s in segs)


# --- step 7: real-cache smoke ---

REAL_CACHE = Path(
    "C:/Users/taile/Videos/ai_video_editor_output/riot/6c643d08-399a-4bc8-889c-0aeb9c73946d"
)


@pytest.mark.skipif(not (REAL_CACHE / "timeline.json").exists(), reason="no real Riot cache")
def test_real_cache_smoke():
    # Parsed straight from the cache files (load_cached also gates on RIOT_ID,
    # which varies per machine).
    m = json.loads((REAL_CACHE / "match.json").read_text(encoding="utf-8"))
    raw_tl = json.loads((REAL_CACHE / "timeline.json").read_text(encoding="utf-8"))
    meta = json.loads((REAL_CACHE / "meta.json").read_text(encoding="utf-8"))
    tl = parse_timeline(
        m,
        raw_tl,
        owner_puuid=meta["owner_puuid"],
        offset_s=float(meta["offset_s"]),
        confidence="high",
    )
    cached = (m, raw_tl)
    segs = early_segments(tl, cached[1])
    assert "opening" in kinds(segs)
    assert all(a.end_s < b.start_s for a, b in itertools.pairwise(segs))
    back = first_back_s(cached[1], tl.owner_pid, tl.offset_s)
    assert back is not None and "first_back" in kinds(segs)
