"""Riot MATCH-V5 timeline: per-asset cache, typed parse, fights, drift check.

The candidate path (`candidates/riot.py`) already fetches the match and
its timeline; this module keeps them on disk per asset
(`WORKSPACE/riot/<asset_id>/`) and turns them into a typed
`RiotTimeline` whose events carry recording seconds and readable actors
("you (Jhin)", "ally Thresh", "enemy Diana").

Confidence gates reuse: a cached `low` correlation is never trusted on a
later run (a wrong match cached would become ground truth), so `fetch`
is called again.

Everything here is pure except the small cache I/O in `load_or_fetch` /
`record_offset`; the fetch itself is injected.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from ..config import settings

_log = logging.getLogger(__name__)

EventKind = Literal[
    "kill", "death", "assist", "ally_death", "enemy_death", "building", "elite", "game_end"
]
Confidence = Literal["high", "medium", "low"]

FIGHT_KINDS = frozenset({"kill", "death", "assist", "ally_death", "enemy_death"})
DEFAULT_PRE_PAD_S = 4.0
WIDE_PRE_PAD_S = 8.0
DRIFT_WARN_S = 3.0


@dataclass
class TimelineEvent:
    kind: EventKind
    t_game_s: float
    t_rec_s: float | None
    actors: list[str]
    detail: dict


@dataclass
class OwnerFrame:
    minute: int
    gold: int
    cs: int
    level: int


@dataclass
class RiotTimeline:
    match_id: str
    confidence: Confidence
    offset_s: float
    game_end_s: float | None
    events: list[TimelineEvent]
    owner_frames: list[OwnerFrame]
    pre_pad_s: float = DEFAULT_PRE_PAD_S


@dataclass
class Fight:
    start_s: float
    end_s: float
    events: list[TimelineEvent] = field(default_factory=list)


# --- parsing (pure) ---


def actor_label(pid: int, participants: dict[int, dict], owner_pid: int) -> str:
    """`you (Jhin)` / `ally Thresh` / `enemy Diana`; `unknown` for non-champions
    (pid 0 = minions/turrets)."""
    p = participants.get(pid)
    if p is None:
        return "unknown"
    champ = p.get("championName") or "?"
    if pid == owner_pid:
        return f"you ({champ})"
    owner_team = participants.get(owner_pid, {}).get("teamId")
    side = "ally" if p.get("teamId") == owner_team else "enemy"
    return f"{side} {champ}"


def _participants(match: dict) -> dict[int, dict]:
    out: dict[int, dict] = {}
    for p in match.get("info", {}).get("participants", []):
        pid = p.get("participantId")
        if pid is not None:
            out[int(pid)] = {"championName": p.get("championName"), "teamId": p.get("teamId")}
    return out


def _owner_pid(match: dict, owner_puuid: str) -> int:
    for p in match.get("info", {}).get("participants", []):
        if p.get("puuid") == owner_puuid and p.get("participantId") is not None:
            return int(p["participantId"])
    raise ValueError("owner puuid not found among match participants")


def _champion_kill(ev: dict, owner: int, parts: dict[int, dict]) -> tuple[EventKind, list, dict]:
    killer = int(ev.get("killerId") or 0)
    victim = int(ev.get("victimId") or 0)
    assists = [int(a) for a in ev.get("assistingParticipantIds") or []]
    if killer == owner:
        kind: EventKind = "kill"
    elif victim == owner:
        kind = "death"
    elif owner in assists:
        kind = "assist"
    elif parts.get(victim, {}).get("teamId") == parts.get(owner, {}).get("teamId"):
        kind = "ally_death"
    else:
        kind = "enemy_death"

    def label(pid: int) -> str:
        return actor_label(pid, parts, owner)

    actors = [label(killer), label(victim), *(label(a) for a in assists)]
    detail = {
        "killer": label(killer),
        "victim": label(victim),
        "assists": [label(a) for a in assists],
    }
    return kind, actors, detail


def parse_timeline(
    match: dict, timeline: dict, *, owner_puuid: str, offset_s: float, confidence: str
) -> RiotTimeline:
    """Typed view of a MATCH-V5 match + timeline in recording seconds
    (`t_rec_s = t_game_s + offset_s`). Raises ValueError if the owner is
    not a participant."""
    owner = _owner_pid(match, owner_puuid)
    parts = _participants(match)
    frames = timeline.get("info", {}).get("frames", [])
    events: list[TimelineEvent] = []
    owner_frames: list[OwnerFrame] = []

    def add(kind: EventKind, ts_ms: float, actors: list[str], detail: dict) -> None:
        t = float(ts_ms) / 1000.0
        events.append(TimelineEvent(kind, t, round(t + offset_s, 3), actors, detail))

    for frame in frames:
        pf = (frame.get("participantFrames") or {}).get(str(owner))
        if pf is not None:
            owner_frames.append(
                OwnerFrame(
                    minute=round(float(frame.get("timestamp", 0)) / 60000.0),
                    gold=int(pf.get("totalGold", 0)),
                    cs=int(pf.get("minionsKilled", 0)) + int(pf.get("jungleMinionsKilled", 0)),
                    level=int(pf.get("level", 0)),
                )
            )
        for ev in frame.get("events", []):
            etype = ev.get("type")
            ts = ev.get("timestamp", 0)
            if etype == "CHAMPION_KILL":
                kind, actors, detail = _champion_kill(ev, owner, parts)
                add(kind, ts, actors, detail)
            elif etype == "BUILDING_KILL":
                killer = actor_label(int(ev.get("killerId") or 0), parts, owner)
                detail = {k: ev.get(k) for k in ("buildingType", "towerType", "laneType")}
                detail["teamId"] = ev.get("teamId")
                add("building", ts, [killer], detail)
            elif etype == "ELITE_MONSTER_KILL":
                killer = actor_label(int(ev.get("killerId") or 0), parts, owner)
                detail = {"monsterType": ev.get("monsterType")}
                if ev.get("monsterSubType"):
                    detail["monsterSubType"] = ev["monsterSubType"]
                detail["killerTeamId"] = ev.get("killerTeamId")
                add("elite", ts, [killer], detail)
            elif etype == "GAME_END":
                add("game_end", ts, [], _end_detail(ev.get("winningTeam"), owner, parts))

    if not any(e.kind == "game_end" for e in events):
        info = match.get("info", {})
        start, end = info.get("gameStartTimestamp"), info.get("gameEndTimestamp")
        if start is not None and end is not None:
            add("game_end", int(end) - int(start), [], _end_detail(None, owner, parts))

    events.sort(key=lambda e: e.t_game_s)
    end_ev = next((e for e in events if e.kind == "game_end"), None)
    return RiotTimeline(
        match_id=str(match.get("metadata", {}).get("matchId") or ""),
        confidence=confidence,  # type: ignore[arg-type]
        offset_s=float(offset_s),
        game_end_s=end_ev.t_rec_s if end_ev else None,
        events=events,
        owner_frames=owner_frames,
    )


def _end_detail(winning_team, owner: int, parts: dict[int, dict]) -> dict:
    owner_team = parts.get(owner, {}).get("teamId")
    won = None if winning_team is None else winning_team == owner_team
    return {"winning_team": winning_team, "won": won}


# --- fights (pure) ---


def fights(events: list[TimelineEvent], *, gap_s: float = 15.0) -> list[Fight]:
    """Chain champion-kill events whose `t_rec_s` is within `gap_s` of the
    previous included event. Buildings, elites and game end never start or
    join a fight."""
    chained = sorted(
        (e for e in events if e.kind in FIGHT_KINDS and e.t_rec_s is not None),
        key=lambda e: e.t_rec_s,
    )
    out: list[Fight] = []
    for e in chained:
        if out and e.t_rec_s - out[-1].end_s <= gap_s:
            out[-1].end_s = e.t_rec_s
            out[-1].events.append(e)
        else:
            out.append(Fight(e.t_rec_s, e.t_rec_s, [e]))
    return out


def fight_containing(fight_list: list[Fight], t_s: float, tol: float = 0.05) -> Fight | None:
    for f in fight_list:
        if f.start_s - tol <= t_s <= f.end_s + tol:
            return f
    return None


# --- offset drift (pure) ---


def offset_drift_s(align: Callable[[tuple[float, float]], float], *, duration_s: float) -> float:
    """|offset(first half) - offset(second half)|. A pause or desync mid-game
    shows up as disagreement between the halves."""
    half = duration_s / 2.0
    return abs(align((0.0, half)) - align((half, duration_s)))


def pre_pad_for_drift(drift_s: float) -> float:
    return WIDE_PRE_PAD_S if drift_s > DRIFT_WARN_S else DEFAULT_PRE_PAD_S


# --- cache ---

FetchResult = tuple  # (match, timeline, match_id, confidence, offset_s[, owner_puuid])


def cache_dir_for(asset_id: str) -> Path:
    return Path(settings.workspace_dir) / "riot" / asset_id


def save_cache(
    cache_dir: Path,
    match: dict,
    timeline: dict,
    *,
    match_id: str,
    confidence: str,
    offset_s: float,
    owner_puuid: str,
) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "match.json").write_text(json.dumps(match), encoding="utf-8")
    (cache_dir / "timeline.json").write_text(json.dumps(timeline), encoding="utf-8")
    meta = {
        "match_id": match_id,
        "confidence": confidence,
        "offset_s": float(offset_s),
        "owner_puuid": owner_puuid,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    (cache_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")


def load_cached(cache_dir: Path) -> tuple[dict, dict, dict] | None:
    """(match, timeline, meta) for a reusable cache entry; None when missing,
    unreadable, or low confidence (never reused)."""
    try:
        meta = json.loads((cache_dir / "meta.json").read_text(encoding="utf-8"))
        if meta.get("confidence") not in ("high", "medium"):
            return None
        match = json.loads((cache_dir / "match.json").read_text(encoding="utf-8"))
        timeline = json.loads((cache_dir / "timeline.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return match, timeline, meta


def record_offset(asset_id: str, offset_s: float, *, cache_dir: Path | None = None) -> None:
    """Store the final calibrated offset in an existing cache entry so later
    `load_or_fetch` callers get recording seconds that match the clips.
    No-op when nothing is cached."""
    d = cache_dir or cache_dir_for(asset_id)
    path = d / "meta.json"
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    meta["offset_s"] = float(offset_s)
    path.write_text(json.dumps(meta, indent=2), encoding="utf-8")


def owner_puuid_from_riot_id(match: dict, riot_id: str) -> str | None:
    name, _, tag = riot_id.partition("#")
    for p in match.get("info", {}).get("participants", []):
        if (p.get("riotIdGameName") or "").lower() == name.lower() and (
            p.get("riotIdTagline") or ""
        ).lower() == tag.lower():
            return p.get("puuid")
    return None


def load_or_fetch(
    asset_id: str,
    *,
    fetch: Callable[[], FetchResult | None],
    cache_dir: Path | None = None,
) -> RiotTimeline | None:
    """Cached `RiotTimeline` for an asset, else `fetch()` and cache it.

    `fetch` returns `(match, timeline, match_id, confidence, offset_s)` and
    may append `owner_puuid` as a sixth element; without it the owner is
    found by `settings.riot_id` among the participants. Returns None when
    the fetch fails or the owner cannot be resolved.
    """
    d = cache_dir or cache_dir_for(asset_id)
    cached = load_cached(d)
    if cached is not None:
        match, timeline, meta = cached
        try:
            return parse_timeline(
                match,
                timeline,
                owner_puuid=meta.get("owner_puuid") or "",
                offset_s=float(meta.get("offset_s") or 0.0),
                confidence=meta["confidence"],
            )
        except ValueError:
            pass  # corrupt/foreign cache entry -> refetch

    got = fetch()
    if got is None:
        return None
    match, timeline, match_id, confidence, offset_s = got[:5]
    owner = got[5] if len(got) > 5 else owner_puuid_from_riot_id(match, settings.riot_id)
    if not owner:
        _log.warning("Riot timeline: owner not resolvable for asset %s", asset_id)
        return None
    try:
        tl = parse_timeline(
            match, timeline, owner_puuid=owner, offset_s=offset_s, confidence=confidence
        )
    except ValueError as e:
        _log.warning("Riot timeline: %s (asset %s)", e, asset_id)
        return None
    try:
        save_cache(
            d,
            match,
            timeline,
            match_id=match_id,
            confidence=confidence,
            offset_s=offset_s,
            owner_puuid=owner,
        )
    except OSError as e:
        _log.warning("Riot timeline cache write failed for %s: %s", asset_id, e)
    return tl
