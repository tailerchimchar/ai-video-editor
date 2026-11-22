"""Per-game multikill scorecard from cached Riot timelines.

    league-scorecard [--asset ID]

One line per game (every cached timeline under WORKSPACE/riot/ by default),
most multikill fights first: which games are worth a story video.
Times are recording seconds (`t_rec_s`).
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from pathlib import Path

from ..config import settings
from .riot_timeline import RiotTimeline, cache_dir_for, fights, load_cached, parse_timeline

MULTIKILL_GAP_S = 10.0  # owner kills this close chain into one multikill
MULTIKILL_FIGHT_MIN = 3  # enemy deaths in one fight
ACE_DEATHS = 5
_ENEMY_DEATH_KINDS = frozenset({"kill", "assist", "enemy_death"})


@dataclass
class GameScore:
    match_id: str
    champion: str
    kda: str
    result: str
    duration_s: float
    multikill_fights: list[dict] = field(default_factory=list)
    aces: int = 0
    owner_multikills: list[dict] = field(default_factory=list)


def _owner(match: dict, owner_puuid: str | None) -> dict:
    parts = match.get("info", {}).get("participants", [])
    if owner_puuid is not None:
        for p in parts:
            if p.get("puuid") == owner_puuid:
                return p
        raise ValueError("owner puuid not found among match participants")
    raise ValueError("owner_puuid is required")


def _owner_multikills(kills: list[float]) -> list[dict]:
    out: list[dict] = []
    run: list[float] = []
    for t in [*sorted(kills), float("inf")]:
        if run and t - run[-1] > MULTIKILL_GAP_S:
            if len(run) >= 2:
                out.append({"t_s": run[0], "count": len(run)})
            run = []
        run.append(t)
    return out


def game_scorecard(
    timeline: RiotTimeline, match: dict, *, owner_puuid: str | None = None
) -> GameScore:
    """Multikill picture of one game. KDA, champion and result come from
    `match`; `owner_puuid` picks the owner (CLI passes it from meta.json). When
    omitted, the owner is the participant whose champion the timeline labels
    "you (<champion>)"."""
    if owner_puuid is None:
        you = next((a for e in timeline.events for a in e.actors if a.startswith("you (")), None)
        champ = you[5:-1] if you else None
        p = next(
            (
                q
                for q in match.get("info", {}).get("participants", [])
                if champ and q.get("championName") == champ
            ),
            None,
        )
        if p is None:
            raise ValueError("owner not identifiable: pass owner_puuid")
    else:
        p = _owner(match, owner_puuid)

    fight_rows: list[dict] = []
    aces = 0
    for f in fights(timeline.events):
        deaths = sum(1 for e in f.events if e.kind in _ENEMY_DEATH_KINDS)
        if deaths >= ACE_DEATHS:
            aces += 1
        owner_kills = sum(1 for e in f.events if e.kind == "kill")
        if deaths >= MULTIKILL_FIGHT_MIN and owner_kills:
            fight_rows.append(
                {
                    "start_s": f.start_s,
                    "end_s": f.end_s,
                    "enemy_deaths": deaths,
                    "owner_kills": owner_kills,
                    "owner_assists": sum(1 for e in f.events if e.kind == "assist"),
                }
            )
    kill_times = [e.t_rec_s for e in timeline.events if e.kind == "kill" and e.t_rec_s is not None]
    return GameScore(
        match_id=timeline.match_id,
        champion=str(p.get("championName") or "?"),
        kda=f"{p.get('kills', 0)}/{p.get('deaths', 0)}/{p.get('assists', 0)}",
        result="WIN" if p.get("win") else "LOSS",
        duration_s=float(match.get("info", {}).get("gameDuration") or 0),
        multikill_fights=fight_rows,
        aces=aces,
        owner_multikills=_owner_multikills(kill_times),
    )


def _score_cache(cache_dir: Path) -> GameScore | None:
    cached = load_cached(cache_dir)
    if cached is None:
        return None
    match, timeline, meta = cached
    try:
        tl = parse_timeline(
            match,
            timeline,
            owner_puuid=meta["owner_puuid"],
            offset_s=float(meta.get("offset_s") or 0.0),
            confidence=meta["confidence"],
        )
        return game_scorecard(tl, match, owner_puuid=meta["owner_puuid"])
    except (ValueError, KeyError, TypeError):
        return None


def _line(s: GameScore) -> str:
    mm, ss = divmod(int(s.duration_s), 60)
    mk = ",".join(str(f["enemy_deaths"]) for f in s.multikill_fights) or "-"
    om = ",".join(f"{m['count']}@{m['t_s']:.0f}" for m in s.owner_multikills) or "-"
    return (
        f"{s.match_id}  {s.champion}  {s.kda}  {s.result}  {mm}:{ss:02d}  "
        f"{len(s.multikill_fights)}({mk})  {s.aces}  {om}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="league-scorecard", description=__doc__)
    parser.add_argument("--asset", default=None, help="only this asset's cached timeline")
    args = parser.parse_args(argv)

    root = Path(settings.workspace_dir) / "riot"
    if args.asset:
        dirs = [cache_dir_for(args.asset)]
    else:
        dirs = sorted(p for p in root.iterdir() if p.is_dir()) if root.is_dir() else []
    scores = [s for d in dirs if (s := _score_cache(d)) is not None]
    scores.sort(key=lambda s: len(s.multikill_fights), reverse=True)
    for s in scores:
        print(_line(s))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
