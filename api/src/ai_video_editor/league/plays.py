"""Riot-only play proposer: chained kill fights become typed `Play` records.

Pure and free. Sides, zones, owner roles and outcomes come from the Riot
timeline alone; `window` stays `vlm_pending` and `numbers` is a lower bound
until a later confirmer fills them."""

from __future__ import annotations

import re

from .map_zones import near_tower, zone_of, zone_relative
from .phases import phase_of
from .play_models import (
    Impressive,
    ObjectiveEvent,
    Phases,
    Play,
    PlayFlags,
    PlayOwner,
    PlayResult,
    PlaySides,
    PlayTimes,
    PlayType,
    PlayWindow,
)
from .riot_timeline import Fight, RiotTimeline, TimelineEvent, fights

AFTER_S = 30.0
CLOSEOUT_S = 60.0
DIVE_RADIUS = 800.0
_LANES = frozenset({"mid_lane", "top_lane", "bot_lane"})
_PITS = frozenset({"dragon_pit", "baron_pit"})
_LABEL = re.compile(r"^(?:you \((?P<me>.+)\)|ally (?P<ally>.+)|enemy (?P<enemy>.+))$")


def _side_and_champ(label: str) -> tuple[str, str] | None:
    """('ally'|'enemy', champion) for a timeline label, None for `unknown`."""
    m = _LABEL.match(label)
    if m is None:
        return None
    if m.group("enemy"):
        return "enemy", m.group("enemy")
    return "ally", m.group("me") or m.group("ally")


class _Roster:
    """Who touched the kills of one fight, keyed by participant id."""

    def __init__(self, evs: list[TimelineEvent]) -> None:
        self.side: dict[int, str] = {}
        self.champ: dict[int, str] = {}
        for e in evs:
            d = e.detail
            pairs = [(d.get("killer_pid"), d.get("killer")), (d.get("victim_pid"), d.get("victim"))]
            pairs += list(zip(d.get("assist_pids") or [], d.get("assists") or [], strict=False))
            for pid, label in pairs:
                sc = _side_and_champ(label or "")
                if pid and sc is not None:
                    self.side[pid], self.champ[pid] = sc

    def names(self, side: str) -> list[str]:
        return sorted(c for p, c in self.champ.items() if self.side[p] == side)

    def pids(self, side: str) -> set[int]:
        return {p for p, s in self.side.items() if s == side}


def _elite_label(e: TimelineEvent) -> str:
    sub = e.detail.get("monsterSubType")
    return f"{e.detail.get('monsterType')}/{sub}" if sub else str(e.detail.get("monsterType"))


def _building_label(e: TimelineEvent) -> str:
    lane = e.detail.get("laneType")
    return f"{e.detail.get('buildingType')}/{lane}" if lane else str(e.detail.get("buildingType"))


def _after(
    timeline: RiotTimeline, last_rec_s: float
) -> tuple[list[TimelineEvent], list[TimelineEvent], bool]:
    """(elite kills, buildings, soul given) within 30 s after the last kill."""
    elites: list[TimelineEvent] = []
    buildings: list[TimelineEvent] = []
    soul = False
    for e in timeline.events:
        if e.t_rec_s is None or not (last_rec_s <= e.t_rec_s <= last_rec_s + AFTER_S):
            continue
        if e.kind == "elite":
            if e.detail.get("monsterType") == "DRAGON_SOUL":
                soul = True
            else:
                elites.append(e)
        elif e.kind == "building":
            buildings.append(e)
    return elites, buildings, soul


def _owner_role(first: TimelineEvent, owner_pid: int | None, involved: bool) -> str:
    if not involved or owner_pid is None:
        return "absent"
    if first.detail.get("killer_pid") == owner_pid:
        return "initiator_kill"
    if first.detail.get("victim_pid") == owner_pid:
        return "caught_first"
    return "follow_up"


def _classify(
    *,
    ed: int,
    ad: int,
    n: int,
    n_allies: int,
    n_events: int,
    zone: str,
    phase: str,
    pos: dict | None,
    to_game_end_s: float | None,
    elite_after: bool,
) -> PlayType:
    """First matching rule wins; `trade` is never proposed from kills."""
    if ed >= 3 and to_game_end_s is not None and to_game_end_s <= CLOSEOUT_S:
        return "closeout"
    if ed >= 3 and n >= 6:
        return "teamfight"
    if elite_after or zone in _PITS:
        return "objective_fight"
    if pos and near_tower(pos["x"], pos["y"], DIVE_RADIUS) is not None:
        return "dive"
    if phase == "early" and n_events == 1 and n == 3 and zone in _LANES:
        return "gank"
    if n_events == 1 and ad == 1 and ed == 0 and n_allies == 1:
        return "caught_out"
    return "skirmish"


def _build_play(idx: int, fight: Fight, timeline: RiotTimeline, phases: Phases) -> Play:
    evs = sorted(fight.events, key=lambda e: e.t_rec_s)
    first, last = evs[0], evs[-1]
    owner_pid = timeline.owner_pid
    roster = _Roster(evs)
    allies, enemies = roster.names("ally"), roster.names("enemy")

    ed = ad = 0
    for e in evs:
        sc = _side_and_champ(e.detail.get("victim") or "")
        if sc is not None:
            if sc[0] == "enemy":
                ed += 1
            else:
                ad += 1
    touched = set(roster.side)
    involved = owner_pid in touched
    pos = first.detail.get("position")
    zone = zone_of(pos["x"], pos["y"]) if pos else zone_of(None, None)
    to_end = None if timeline.game_end_s is None else timeline.game_end_s - last.t_rec_s
    elites, buildings, soul = _after(timeline, last.t_rec_s)
    # A closeout belongs to the phase holding the game end.
    phase_t = last.t_game_s if to_end is not None and to_end <= CLOSEOUT_S else first.t_game_s
    phase = phase_of(phases, phase_t)

    ptype = _classify(
        ed=ed,
        ad=ad,
        n=len(touched),
        n_allies=len(allies),
        n_events=len(evs),
        zone=zone,
        phase=phase,
        pos=pos,
        to_game_end_s=to_end,
        elite_after=bool(elites),
    )

    ally_n, enemy_n = len(allies), len(enemies)
    majority = None
    if timeline.owner_team_id is not None and ally_n != enemy_n:
        other = 200 if timeline.owner_team_id == 100 else 100
        majority = timeline.owner_team_id if ally_n > enemy_n else other
    steal = majority is not None and any(
        e.detail.get("killerTeamId") not in (None, majority) for e in elites
    )
    outcome = "won" if ed > ad else "lost" if ed < ad else "even"

    play = Play(
        id=f"p{idx:02d}",
        type=ptype,
        phase=phase,
        game_time=PlayTimes(first_kill_s=first.t_game_s, last_kill_s=last.t_game_s),
        recording_time=PlayTimes(first_kill_s=first.t_rec_s, last_kill_s=last.t_rec_s),
        window=PlayWindow(),
        zone=zone,
        zone_relative=zone_relative(zone, timeline.owner_team_id),
        position=pos,
        sides=PlaySides(allies=allies, enemies=enemies, numbers=f"{ally_n}v{enemy_n}"),
        owner=PlayOwner(
            involved=involved,
            role=_owner_role(first, owner_pid, involved),  # type: ignore[arg-type]
            kills=sum(e.kind == "kill" for e in evs),
            assists=sum(e.kind == "assist" for e in evs),
            died=any(e.kind == "death" for e in evs),
        ),
        result=PlayResult(
            enemy_deaths=ed,
            ally_deaths=ad,
            outcome=outcome,  # type: ignore[arg-type]
            objective_after=[_elite_label(e) for e in elites],
            building_after=[_building_label(e) for e in buildings],
        ),
        flags=PlayFlags(
            help_from_team=len(roster.pids("ally")) >= 2,
            soul_point=True if soul else None,
            steal=steal,
        ),
        impressive=Impressive(),
    )
    play.text = play_text(play)
    return play


def propose_plays(timeline: RiotTimeline, *, phases: Phases, gap_s: float = 15.0) -> list[Play]:
    """One `Play` per kill fight, ids `p01`.. in time order."""
    return [
        _build_play(i, f, timeline, phases)
        for i, f in enumerate(fights(timeline.events, gap_s=gap_s), start=1)
    ]


def objectives_from(timeline: RiotTimeline) -> list[ObjectiveEvent]:
    """Elite kills and buildings taken, in game seconds; dragon souls skipped
    (a soul is a consequence of dragons, not an objective of its own)."""
    owner_team = timeline.owner_team_id
    out: list[ObjectiveEvent] = []
    for e in timeline.events:
        if e.kind == "elite" and e.detail.get("monsterType") != "DRAGON_SOUL":
            taker = e.detail.get("killerTeamId")
            team = "ally" if owner_team is not None and taker == owner_team else "enemy"
            out.append(
                ObjectiveEvent(
                    t_s=e.t_game_s,
                    type=str(e.detail.get("monsterType")),
                    sub=e.detail.get("monsterSubType"),
                    team=team,
                )
            )
        elif e.kind == "building":
            # `teamId` owns the destroyed building: the other side took it.
            lost_by = e.detail.get("teamId")
            team = "enemy" if owner_team is not None and lost_by == owner_team else "ally"
            out.append(
                ObjectiveEvent(
                    t_s=e.t_game_s,
                    type=str(e.detail.get("buildingType")),
                    sub=e.detail.get("laneType"),
                    team=team,
                )
            )
    return out


def play_text(play: Play) -> str:
    """`5v4 teamfight in mid_lane, owner initiator_kill, won 4-for-1[, then X]`."""
    r = play.result
    after = f", then {r.objective_after[0]}" if r.objective_after else ""
    return (
        f"{play.sides.numbers} {play.type} in {play.zone_relative}, owner {play.owner.role}, "
        f"{r.outcome} {r.enemy_deaths}-for-{r.ally_deaths}{after}"
    )


def rank_plays(plays: list[Play]) -> list[Play]:
    """Best first: owner-absent last, then numbers faced (unknown last), owner
    kills, enemy deaths, earlier first. Sets `impressive.rank` 1..n."""

    def key(p: Play):
        nf = p.impressive.numbers_faced
        return (
            p.owner.role == "absent",
            nf is None,
            -(nf or 0),
            -p.owner.kills,
            -p.result.enemy_deaths,
            p.game_time.first_kill_s,
        )

    ordered = sorted(plays, key=key)
    for rank, p in enumerate(ordered, start=1):
        p.impressive.rank = rank
    return ordered
