"""Lane summary: the owner vs their same-position enemy through minute ~14.

Pure parse of the cached MATCH-V5 match + timeline (`parse_lane_frames`).
The opponent is identified by champion only; no Riot ID or summoner name
ever enters the summary (R9). `lane.json` sits next to the other cache
files under `WORKSPACE/riot/<asset_id>/`.
"""

from __future__ import annotations

from pathlib import Path

from .lane_models import LaneMinute, LaneSummary
from .riot_timeline import _owner_pid, cache_dir_for

__all__ = ["LaneMinute", "LaneSummary", "lane_summary_path", "parse_lane_frames"]

LANE_FILE = "lane.json"


def lane_summary_path(asset_id: str) -> Path:
    return cache_dir_for(asset_id) / LANE_FILE


def _stats(pf: dict) -> tuple[int, int, int]:
    cs = int(pf.get("minionsKilled", 0)) + int(pf.get("jungleMinionsKilled", 0))
    return cs, int(pf.get("totalGold", 0)), int(pf.get("level", 0))


def _check_index(by_pid: dict[int, dict], timeline: dict) -> None:
    """Cross-check timeline participantId -> puuid against the match. Raises
    ValueError on any disagreement; without a timeline participants list the
    per-frame check (every frame pid maps to a match champion) still applies."""
    for tp in timeline.get("info", {}).get("participants") or []:
        pid, puuid = tp.get("participantId"), tp.get("puuid")
        mp = by_pid.get(int(pid)) if pid is not None else None
        if mp is None or (puuid and mp.get("puuid") != puuid):
            raise ValueError(f"timeline participant {pid} does not match the match's participants")


def parse_lane_frames(
    match: dict, timeline: dict, *, owner_puuid: str, offset_s: float, through_minute: int = 14
) -> LaneSummary | None:
    """Owner vs the enemy with the same `teamPosition`, per minute through
    `through_minute`. None when there is no such opponent (e.g. ARAM).
    Raises ValueError when the timeline's participant ids disagree with the
    match's, or a frame id maps to no champion. A minute with no frame is
    skipped, but a frame within range that lacks the owner's or opponent's
    participant frame raises."""
    participants = match.get("info", {}).get("participants", [])
    owner = _owner_pid(match, owner_puuid)
    by_pid = {
        int(p["participantId"]): p for p in participants if p.get("participantId") is not None
    }
    me = by_pid[owner]
    position = me.get("teamPosition")
    if not position:
        return None
    opp = next(
        (
            p
            for p in participants
            if p.get("teamPosition") == position
            and p.get("teamId") != me.get("teamId")
            and p.get("participantId") is not None
        ),
        None,
    )
    if opp is None:
        return None
    opp_pid = int(opp["participantId"])
    champion = opp.get("championName")
    if not champion or not me.get("championName"):
        raise ValueError("participant index does not map to a champion in the match")
    _check_index(by_pid, timeline)

    minutes: list[LaneMinute] = []
    first_blood_s: float | None = None
    owner_first_blood = False
    first_lane_kill_s: float | None = None
    lane_pids = {owner, opp_pid}

    for frame in timeline.get("info", {}).get("frames", []):
        pfs = frame.get("participantFrames") or {}
        for key in pfs:
            if int(key) not in by_pid or not by_pid[int(key)].get("championName"):
                raise ValueError(f"timeline participant {key} not found among match champions")
        minute = round(float(frame.get("timestamp", 0)) / 60000.0)
        if minute <= through_minute:
            if str(owner) not in pfs or str(opp_pid) not in pfs:
                raise ValueError(f"timeline minute {minute} lacks lane participant frames")
            o_cs, o_gold, o_lvl = _stats(pfs[str(owner)])
            p_cs, p_gold, p_lvl = _stats(pfs[str(opp_pid)])
            minutes.append(LaneMinute(minute, o_cs, o_gold, o_lvl, p_cs, p_gold, p_lvl))
        for ev in frame.get("events", []):
            t = round(float(ev.get("timestamp", 0)) / 1000.0 + offset_s, 3)
            etype = ev.get("type")
            if (
                etype == "CHAMPION_SPECIAL_KILL"
                and ev.get("killType") == "KILL_FIRST_BLOOD"
                and first_blood_s is None
            ):
                first_blood_s = t
                owner_first_blood = int(ev.get("killerId") or 0) == owner
            elif etype == "CHAMPION_KILL" and first_lane_kill_s is None:
                killer, victim = int(ev.get("killerId") or 0), int(ev.get("victimId") or 0)
                if killer in lane_pids or victim in lane_pids:
                    first_lane_kill_s = t

    at10 = next((m for m in minutes if m.minute == 10), None)
    return LaneSummary(
        opponent_champion=champion,
        minutes=minutes,
        cs_diff_at_10=at10.owner_cs - at10.opp_cs if at10 else None,
        gold_diff_at_10=at10.owner_gold - at10.opp_gold if at10 else None,
        first_blood_s=first_blood_s,
        first_lane_kill_s=first_lane_kill_s,
        owner_first_blood=owner_first_blood,
    )
