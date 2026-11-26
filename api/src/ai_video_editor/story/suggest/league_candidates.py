"""League candidate builder: one candidate per owner-involved Riot fight
inside a story chapter.

Facts come from the Riot timeline (kills, deaths, multikill chain, enemy
champions, objective taken by the owner's team) and the frozen plays file
when there is one (`numbers` such as "4v5", whether the fight was won a
player down, objectives after it). Champion names only: actor labels are
"you (X)" / "ally X" / "enemy X" and only enemy champions are kept.
The last candidate of the game is the last fight.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from ...config import settings
from ...league.play_models import GameModel, Play
from ...league.riot_timeline import Fight, RiotTimeline, TimelineEvent, fights
from ..planner import EVENT_POST_S, Chapter, StoryPlan, _outnumbered
from .candidates import register_candidate_builder
from .models import Candidate

OWNER_KINDS = ("kill", "assist", "death")
# League's multikill window: each kill within 10 s of the previous one.
MULTIKILL_GAP_S = 10.0
FIGHT_LEAD_S = 3.0
# An objective taken this soon after a fight counts as that fight's objective.
OBJECTIVE_AFTER_S = 30.0
_ENEMY = "enemy "


def load_plays_file(asset_id: str) -> GameModel | None:
    """`WORKSPACE/plays/<asset>.json`, or None when missing or unreadable."""
    path = Path(settings.workspace_dir) / "plays" / f"{asset_id}.json"
    try:
        return GameModel.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _multikill(kill_times: list[float]) -> int:
    best = run = 0
    last: float | None = None
    for t in sorted(kill_times):
        run = run + 1 if last is not None and t - last <= MULTIKILL_GAP_S else 1
        best = max(best, run)
        last = t
    return best


def _enemies(events: list[TimelineEvent]) -> list[str]:
    names = {a[len(_ENEMY) :] for e in events for a in e.actors if a.startswith(_ENEMY)}
    return sorted(n for n in names if n and n != "?")


def _objective(tl: RiotTimeline, fight: Fight, play: Play | None) -> str | None:
    if play is not None and play.result.objective_after:
        return play.result.objective_after[0].lower()
    for e in tl.events:
        if (
            e.kind == "elite"
            and e.t_rec_s is not None
            and fight.start_s <= e.t_rec_s <= fight.end_s + OBJECTIVE_AFTER_S
            and e.detail.get("killerTeamId") == tl.owner_team_id
            and e.detail.get("monsterType")
        ):
            return str(e.detail["monsterType"]).lower()
    return None


def _play_for(fight: Fight, chapter_fights: list[Fight], play: Play | None) -> Play | None:
    """The chapter's play describes the fight overlapping its kills (else the
    chapter's last fight)."""
    if play is None:
        return None
    lo, hi = play.game_time.first_kill_s - 1.0, play.game_time.last_kill_s + 1.0
    overlapping = [f for f in chapter_fights if any(lo <= e.t_game_s <= hi for e in f.events)]
    target = overlapping[0] if overlapping else chapter_fights[-1]
    return play if target is fight else None


class LeagueCandidateBuilder:
    def __init__(self, load_plays: Callable[[str], GameModel | None] = load_plays_file) -> None:
        self._load_plays = load_plays

    def build(self, plan: StoryPlan, timeline: RiotTimeline) -> list[Candidate]:
        game = self._load_plays(plan.asset_id) if plan.asset_id else None
        plays = {p.id: p for p in (game.plays if game else [])}
        all_fights = fights(timeline.events)
        out: list[Candidate] = []
        for index, chapter in enumerate(plan.chapters):
            mine = _chapter_fights(chapter, all_fights)
            play = plays.get(chapter.play_id) if chapter.play_id else None
            for fight in mine:
                out.append(
                    _candidate(index, chapter, fight, timeline, _play_for(fight, mine, play))
                )
        if out:
            out[-1].facts["is_last_fight"] = True
        return out

    def chapter_spans(
        self, plan: StoryPlan, timeline: RiotTimeline
    ) -> dict[int, tuple[float, float]]:
        off = timeline.offset_s
        return {
            i: (round(c.start_s - off, 2), round(c.end_s - off, 2))
            for i, c in enumerate(plan.chapters)
        }


def _chapter_fights(chapter: Chapter, all_fights: list[Fight]) -> list[Fight]:
    """Fights with an owner kill/assist/death inside the chapter, cut to it."""
    out = []
    for f in all_fights:
        inside = [e for e in f.events if chapter.start_s <= e.t_rec_s <= chapter.end_s]
        if any(e.kind in OWNER_KINDS for e in inside):
            out.append(Fight(inside[0].t_rec_s, inside[-1].t_rec_s, inside))
    return out


def _candidate(
    index: int, chapter: Chapter, fight: Fight, tl: RiotTimeline, play: Play | None
) -> Candidate:
    offset = tl.offset_s
    start = max(chapter.start_s, fight.start_s - FIGHT_LEAD_S) - offset
    end = min(chapter.end_s, fight.end_s + EVENT_POST_S) - offset
    kills = [e.t_game_s for e in fight.events if e.kind == "kill"]
    numbers = play.sides.numbers if play is not None else None
    facts = {
        "minute": int(fight.events[0].t_game_s // 60),
        "kills": len(kills),
        "deaths": sum(1 for e in fight.events if e.kind == "death"),
        "numbers": numbers,
        "outnumbered_win": bool(
            play is not None and play.result.outcome == "won" and _outnumbered(numbers or "")
        ),
        "multikill": _multikill(kills),
        "objective": _objective(tl, fight, play),
        "is_last_fight": False,
        "enemy_champions": _enemies(fight.events),
    }
    return Candidate(
        id=f"c{index}-{round(start)}",
        chapter_index=index,
        start_s=round(start, 2),
        end_s=round(end, 2),
        facts=facts,
        anchor_s=min(kills) if kills else None,
    )


register_candidate_builder("league", LeagueCandidateBuilder())
