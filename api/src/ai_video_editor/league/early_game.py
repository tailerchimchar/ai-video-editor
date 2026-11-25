"""Early-game windows a League story must show, from the cached Riot timeline.

Pure: no network, no VLM. All times returned are recording seconds
(`t_game + offset_s`, the same conversion `RiotTimeline` uses).

Kinds: `opening` (always), `first_back` (owner's first shop trip after 2:30),
`level3_trade`, `jungle_move`, `low_hp`, `give_recall`. Overlapping segments
are merged (kind chosen by `_PRIORITY`) and nothing ends after first back + 8 s.
"""

from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel

from .map_zones import zone_of
from .riot_timeline import FIGHT_KINDS, RiotTimeline

EarlyKind = Literal["opening", "level3_trade", "jungle_move", "low_hp", "give_recall", "first_back"]

STARTING_ITEMS_END_S = 150.0  # purchases before game 2:30 are starting items
BACK_PRE_S = 12.0
BACK_POST_S = 8.0
GIVE_RECALL_MAX_S = 360.0
SAME_TRIP_S = 20.0

BRAWL_START_S = 50.0
DEFAULT_START_S = 60.0
BRAWL_CHECK_S = 65.0
OPENING_END_S = 120.0
OPENING_END_CAP_S = 150.0
LEVEL2_PAD_S = 10.0
# Bot tri-bush / river-bush region (game coords) where both bot lanes contest.
BUSH_CENTER = (11800.0, 3200.0)
BUSH_RADIUS = 1800.0

TRADE_PRE_S = 15.0
TRADE_POST_S = 20.0
TRADE_KILL_WINDOW_S = 20.0
TRADE_HP_FRAC = 0.5
FRAME_PAD_S = 10.0
LOW_HP_FRAC = 0.25

_PRIORITY = ("first_back", "opening", "level3_trade", "low_hp", "give_recall", "jungle_move")


class EarlySegment(BaseModel):
    kind: EarlyKind
    start_s: float  # recording seconds
    end_s: float  # recording seconds
    reason: str  # templated facts only, no transcript text
    confirmed: bool = False  # set True by the vision check


def _frames(raw_timeline: dict) -> list[dict]:
    info = raw_timeline.get("info", raw_timeline)
    return info.get("frames", []) or []


def _owner_purchases_s(raw_timeline: dict, owner_pid: int) -> list[float]:
    """Game-seconds of the owner's post-starting-item purchases, sorted."""
    out = [
        float(ev["timestamp"]) / 1000.0
        for fr in _frames(raw_timeline)
        for ev in fr.get("events", [])
        if ev.get("type") == "ITEM_PURCHASED"
        and int(ev.get("participantId") or 0) == owner_pid
        and float(ev.get("timestamp", 0)) / 1000.0 > STARTING_ITEMS_END_S
    ]
    return sorted(out)


def first_back_s(raw_timeline: dict, owner_pid: int, offset_s: float) -> float | None:
    """Recording seconds of the owner's first purchase after game 2:30
    (starting items excluded); None when the owner bought nothing then."""
    buys = _owner_purchases_s(raw_timeline, owner_pid)
    return round(buys[0] + offset_s, 3) if buys else None


def _owner_level_up_s(timeline_raw: dict, owner_pid: int, level: int) -> float | None:
    for fr in _frames(timeline_raw):
        for ev in fr.get("events", []):
            if (
                ev.get("type") == "LEVEL_UP"
                and int(ev.get("participantId") or 0) == owner_pid
                and int(ev.get("level") or 0) == level
            ):
                return float(ev["timestamp"]) / 1000.0
    return None


def _in_bush(pos: dict | None) -> bool:
    if not pos or pos.get("x") is None or pos.get("y") is None:
        return False
    return math.hypot(pos["x"] - BUSH_CENTER[0], pos["y"] - BUSH_CENTER[1]) <= BUSH_RADIUS


def _brawl_before_1_05(timeline: RiotTimeline, raw: dict) -> str | None:
    """Why the opening should start at 0:50, else None: a champion kill
    before 1:05, or the owner and an enemy both in the bot bush at the
    frame before 1:05 (bot laners are not labelled in the timeline, so
    any enemy stands in for the enemy bot laner)."""
    if any(e.kind in FIGHT_KINDS and e.t_game_s < BRAWL_CHECK_S for e in timeline.events):
        return "champion kill before 1:05"
    owner = timeline.owner_pid
    enemies = range(6, 11) if timeline.owner_team_id == 100 else range(1, 6)
    for fr in _frames(raw):
        ts = float(fr.get("timestamp", 0)) / 1000.0
        if not 0 < ts < BRAWL_CHECK_S:
            continue
        pfs = fr.get("participantFrames") or {}
        if _in_bush((pfs.get(str(owner)) or {}).get("position")) and any(
            _in_bush((pfs.get(str(e)) or {}).get("position")) for e in enemies
        ):
            return "both bot laners in the bush before 1:05"
    return None


def _opening(timeline: RiotTimeline, raw: dict) -> EarlySegment:
    why = _brawl_before_1_05(timeline, raw)
    start = BRAWL_START_S if why else DEFAULT_START_S
    end = OPENING_END_S
    lvl2 = _owner_level_up_s(raw, timeline.owner_pid, 2)
    if lvl2 is not None:
        end = min(max(end, lvl2 + LEVEL2_PAD_S), OPENING_END_CAP_S)
    reason = f"opening from {int(start) // 60}:{int(start) % 60:02d}"
    reason += f" ({why})" if why else " (no early brawl)"
    return _seg("opening", timeline, start, end, reason)


def _seg(
    kind: EarlyKind, timeline: RiotTimeline, g0: float, g1: float, reason: str
) -> EarlySegment:
    """Segment from game-second bounds, converted to recording seconds."""
    off = timeline.offset_s
    return EarlySegment(
        kind=kind, start_s=round(max(0.0, g0 + off), 3), end_s=round(g1 + off, 3), reason=reason
    )


def _first_back(timeline: RiotTimeline, raw: dict) -> EarlySegment | None:
    buys = _owner_purchases_s(raw, timeline.owner_pid)
    if not buys:
        return None
    t = buys[0]
    return _seg(
        "first_back", timeline, t - BACK_PRE_S, t + BACK_POST_S, f"first back at game {_mmss(t)}"
    )


def _mmss(t: float) -> str:
    return f"{int(t) // 60}:{int(t) % 60:02d}"


def _level3_trade(timeline: RiotTimeline, raw: dict) -> EarlySegment | None:
    owner = timeline.owner_pid
    t = _owner_level_up_s(raw, owner, 3)
    if t is None:
        return None
    near = [
        e.t_game_s
        for e in timeline.events
        if e.kind in ("kill", "death", "assist") and abs(e.t_game_s - t) <= TRADE_KILL_WINDOW_S
    ]
    if near:
        lo, hi = min(t, *near), max(t, *near)
        return _seg(
            "level3_trade", timeline, lo - TRADE_PRE_S, hi + TRADE_POST_S,
            f"level 3 at {_mmss(t)} with a kill/assist/death within {int(TRADE_KILL_WINDOW_S)}s",
        )  # fmt: skip
    for fr in _frames(raw):
        if float(fr.get("timestamp", 0)) / 1000.0 > t:
            frac = _hp_frac(fr, owner)
            if frac is not None and frac <= TRADE_HP_FRAC:
                return _seg(
                    "level3_trade", timeline, t - TRADE_PRE_S, t + TRADE_POST_S,
                    f"level 3 at {_mmss(t)}, health {frac:.0%} in the next minute frame",
                )  # fmt: skip
            break
    return None


def _hp_frac(frame: dict, pid: int) -> float | None:
    stats = ((frame.get("participantFrames") or {}).get(str(pid)) or {}).get("championStats") or {}
    hp, mx = stats.get("health"), stats.get("healthMax")
    if hp is None or not mx:
        return None
    return float(hp) / float(mx)


def _frame_segments(
    timeline: RiotTimeline, raw: dict, after_s: float, before_s: float | None
) -> list[EarlySegment]:
    """jungle_move and low_hp from minute frames after the opening window
    (it already covers them) and strictly before first back."""
    out: list[EarlySegment] = []
    owner = timeline.owner_pid
    for fr in _frames(raw):
        ts = float(fr.get("timestamp", 0)) / 1000.0
        if ts <= after_s or (before_s is not None and ts >= before_s):
            continue
        pf = (fr.get("participantFrames") or {}).get(str(owner)) or {}
        pos = pf.get("position") or {}
        zone = zone_of(pos.get("x"), pos.get("y"))
        if zone.endswith("_jungle") or zone.endswith("_river"):
            out.append(
                _seg("jungle_move", timeline, ts - FRAME_PAD_S, ts + FRAME_PAD_S,
                     f"owner in {zone} at {_mmss(ts)}")
            )  # fmt: skip
        frac = _hp_frac(fr, owner)
        if frac is not None and frac <= LOW_HP_FRAC:
            out.append(
                _seg("low_hp", timeline, ts - FRAME_PAD_S, ts + FRAME_PAD_S,
                     f"owner at {frac:.0%} health at {_mmss(ts)}")
            )  # fmt: skip
    return out


def _give_recalls(timeline: RiotTimeline, raw: dict) -> list[EarlySegment]:
    buys = _owner_purchases_s(raw, timeline.owner_pid)
    if not buys:
        return []
    first = buys[0]
    deaths = [e.t_game_s for e in timeline.events if e.kind == "death"]
    out = []
    for t in buys:
        if t >= GIVE_RECALL_MAX_S or t - first <= SAME_TRIP_S:
            continue
        if any(0 <= t - d <= SAME_TRIP_S for d in deaths):
            continue  # a respawn buy, not a deliberate recall
        out.append(
            _seg("give_recall", timeline, t - BACK_PRE_S, t + BACK_POST_S,
                 f"purchase at {_mmss(t)} after first back")
        )  # fmt: skip
    return out


def _merge(segs: list[EarlySegment]) -> list[EarlySegment]:
    """Merge overlapping segments; the kind with the highest `_PRIORITY`
    wins and the reasons are joined."""
    out: list[EarlySegment] = []
    for s in sorted(segs, key=lambda s: (s.start_s, s.end_s)):
        if out and s.start_s <= out[-1].end_s:
            prev = out[-1]
            kind = min(prev.kind, s.kind, key=_PRIORITY.index)
            reason = prev.reason if s.reason in prev.reason else f"{prev.reason}; {s.reason}"
            out[-1] = EarlySegment(
                kind=kind, start_s=prev.start_s, end_s=max(prev.end_s, s.end_s), reason=reason
            )
        else:
            out.append(s)
    return out


def early_segments(timeline: RiotTimeline, raw_timeline: dict) -> list[EarlySegment]:
    """Early-game windows (recording seconds), merged and sorted. Always
    contains an `opening`; contains `first_back` whenever the owner bought
    anything after 2:30. Everything except `give_recall` (which by
    definition comes after first back) ends no later than first back + 8 s."""
    if timeline.owner_pid is None:
        return []
    raw = raw_timeline
    opening = _opening(timeline, raw)
    segs = [opening]
    buys = _owner_purchases_s(raw, timeline.owner_pid)
    back = _first_back(timeline, raw)
    if back is not None:
        segs.append(back)
    trade = _level3_trade(timeline, raw)
    if trade is not None:
        segs.append(trade)
    segs += _frame_segments(
        timeline, raw, opening.end_s - timeline.offset_s, buys[0] if buys else None
    )
    if buys:
        cap = buys[0] + BACK_POST_S + timeline.offset_s
        segs = [s.model_copy(update={"end_s": min(s.end_s, cap)}) for s in segs]
        segs = [s for s in segs if s.start_s < s.end_s]
    segs += _give_recalls(timeline, raw)
    return _merge(segs)
