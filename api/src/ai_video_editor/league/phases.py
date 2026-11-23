"""Event-driven game phases (game seconds).

`early` ends at the first tower kill or 14:00, never before 8:00. `late`
begins at the first baron or inhibitor kill, else 25:00. A game that ends
before `late` would begin has no `late`. A remake has no phases."""

from __future__ import annotations

from .play_models import PhaseBounds, Phases
from .riot_timeline import RiotTimeline

EARLY_MIN_S = 480.0
EARLY_MAX_S = 840.0
LATE_DEFAULT_S = 1500.0
REMAKE_MAX_S = 300.0


def phases_for(timeline: RiotTimeline) -> Phases | None:
    events = timeline.events
    end_ev = next((e for e in events if e.kind == "game_end"), None)
    game_end = end_ev.t_game_s if end_ev else max((e.t_game_s for e in events), default=0.0)
    has_kill = any(
        e.kind in ("kill", "death", "assist", "ally_death", "enemy_death") for e in events
    )
    if end_ev is not None and game_end < REMAKE_MAX_S and not has_kill:
        return None

    tower_t = next(
        (
            e.t_game_s
            for e in events
            if e.kind == "building" and e.detail.get("buildingType") == "TOWER_BUILDING"
        ),
        None,
    )
    if tower_t is not None and tower_t < EARLY_MAX_S:
        early_end, early_by = max(tower_t, EARLY_MIN_S), "first_tower"
    else:
        early_end, early_by = EARLY_MAX_S, "14:00"
    early_end = min(early_end, game_end)

    late_start, late_by = LATE_DEFAULT_S, "25:00"
    for e in events:
        if e.kind == "elite" and e.detail.get("monsterType") == "BARON_NASHOR":
            late_start, late_by = e.t_game_s, "first_baron"
            break
        if e.kind == "building" and e.detail.get("buildingType") == "INHIBITOR_BUILDING":
            late_start, late_by = e.t_game_s, "first_inhibitor"
            break
    # Baron/inhibitor are caught in time order, so the first one wins.
    late_start = max(late_start, early_end)

    early = PhaseBounds(name="early", start_s=0.0, end_s=early_end, boundary=early_by)
    if late_start >= game_end:
        mid = PhaseBounds(name="mid", start_s=early_end, end_s=game_end, boundary=early_by)
        return Phases(early=early, mid=mid, late=None)
    mid = PhaseBounds(name="mid", start_s=early_end, end_s=late_start, boundary=early_by)
    late = PhaseBounds(name="late", start_s=late_start, end_s=game_end, boundary=late_by)
    return Phases(early=early, mid=mid, late=late)


def phase_of(phases: Phases, t_game_s: float) -> str:
    if t_game_s < phases.early.end_s:
        return "early"
    if phases.late is not None and t_game_s >= phases.late.start_s:
        return "late"
    return "mid"
