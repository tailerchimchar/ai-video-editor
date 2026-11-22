"""Story planner (pure): chronological chapters from expert beats and the Riot timeline.

Every chapter obeys the fight-window rules from `highlights`: a beat inside a
fight widens to the fight padded by FIGHT_PRE_S (FIGHT_PRE_ALLY_DEATH_S when the
fight opens with an ally or owner death) and FIGHT_POST_S. Lane chapters are the
first two owner kill/death events before 14 min; the close chapter is the fight
containing (or nearest to) the game end and ends at game_end + 1 s. Overlapping
chapters merge. Over `max_s`, the lowest-intensity chapter that is neither lane
nor close is dropped first.
"""

from __future__ import annotations

import logging
import re
from typing import Literal

from pydantic import BaseModel, model_validator

from ..highlights import FIGHT_POST_S, FIGHT_PRE_ALLY_DEATH_S, FIGHT_PRE_S
from ..league.lane_models import LaneSummary
from ..league.riot_timeline import Fight, RiotTimeline, TimelineEvent, fights

_log = logging.getLogger(__name__)

TITLE_MAX = 40
# The close uses the last fight only when it ends this close to the nexus;
# otherwise the chapter is the end alone. Never longer than STORY_CLOSE_MAX_S.
STORY_CLOSE_REACH_S = 45.0
STORY_CLOSE_MAX_S = 60.0
_LABEL_PREFIX = re.compile(r"^\w+:\s*")
LANE_BEFORE_S = 14 * 60.0
LANE_EVENTS = 2
GAME_END_TAIL_S = 1.0
CLOSE_TITLE = "The close"
_PROTECTED = ("lane", "close")
_KIND_RANK = {"close": 3, "lane": 2, "fight": 1, "commentary": 0}


class Beat(BaseModel):
    t_start: float
    t_end: float
    beat: str
    kind: Literal["lane", "fight", "close", "commentary"] = "fight"

    @model_validator(mode="after")
    def _ordered(self) -> Beat:
        if self.t_end < self.t_start:
            raise ValueError(f"beat ends ({self.t_end}) before it starts ({self.t_start})")
        return self


class Chapter(BaseModel):
    title: str
    start_s: float
    end_s: float
    kind: str
    beats: list[str] = []


class StoryPlan(BaseModel):
    asset_id: str = ""
    chapters: list[Chapter]
    total_s: float
    mic_stream: int | None
    audio: Literal["mic_over_game", "mixed"]
    # True when lane + close chapters alone exceed max_s (nothing left to drop).
    over_budget: bool = False


def _title(text: str) -> str:
    return text.strip()[:TITLE_MAX].rstrip()


def _beat_title(text: str) -> str:
    """Drop an expert's "setup:" / "payoff:" label before capping the length."""
    return _title(_LABEL_PREFIX.sub("", text.strip()) or text)


def _opens_with_death(events: list[TimelineEvent]) -> bool:
    return bool(events) and events[0].kind in ("ally_death", "death")


def _pre(events: list[TimelineEvent]) -> float:
    return FIGHT_PRE_ALLY_DEATH_S if _opens_with_death(events) else FIGHT_PRE_S


def _fight_window(f: Fight) -> tuple[float, float]:
    return f.start_s - _pre(f.events), f.end_s + FIGHT_POST_S


def _overlapping_fights(fight_list: list[Fight], start: float, end: float) -> list[Fight]:
    return [f for f in fight_list if f.start_s <= end and f.end_s >= start]


def _beat_chapter(b: Beat, fight_list: list[Fight]) -> Chapter:
    """Union of the beat and the padded window of every fight it overlaps."""
    windows = [(b.t_start, b.t_end)]
    windows += [_fight_window(f) for f in _overlapping_fights(fight_list, b.t_start, b.t_end)]
    start, end = min(w[0] for w in windows), max(w[1] for w in windows)
    kind = "fight" if b.kind == "close" else b.kind
    return Chapter(title=_beat_title(b.beat), start_s=start, end_s=end, kind=kind, beats=[b.beat])


def _lane_chapters(tl: RiotTimeline, lane: LaneSummary | None) -> list[Chapter]:
    owner = [
        e
        for e in tl.events
        if e.kind in ("kill", "death") and e.t_rec_s is not None and e.t_game_s < LANE_BEFORE_S
    ]
    label = f"Lane vs {lane.opponent_champion}" if lane else "Lane"
    out = []
    for e in sorted(owner, key=lambda e: e.t_rec_s)[:LANE_EVENTS]:
        pre = FIGHT_PRE_ALLY_DEATH_S if e.kind == "death" else FIGHT_PRE_S
        out.append(
            Chapter(
                title=_title(f"{label}: {e.kind}"),
                start_s=e.t_rec_s - pre,
                end_s=e.t_rec_s + FIGHT_POST_S,
                kind="lane",
            )
        )
    return out


def _close_chapter(tl: RiotTimeline, fight_list: list[Fight]) -> Chapter | None:
    if tl.game_end_s is None:
        return None
    end = tl.game_end_s + GAME_END_TAIL_S
    near = [
        f
        for f in fight_list
        if f.start_s <= tl.game_end_s and tl.game_end_s - f.end_s <= STORY_CLOSE_REACH_S
    ]
    if near:
        f = min(near, key=lambda f: max(0.0, tl.game_end_s - f.end_s))
        start = _fight_window(f)[0]
    else:
        start = tl.game_end_s - FIGHT_PRE_ALLY_DEATH_S
    start = max(start, end - STORY_CLOSE_MAX_S)
    return Chapter(title=CLOSE_TITLE, start_s=start, end_s=end, kind="close")


def _merge(a: Chapter, b: Chapter) -> Chapter:
    lead = a if _KIND_RANK.get(a.kind, 0) >= _KIND_RANK.get(b.kind, 0) else b
    titled = next((c for c in (a, b) if c.beats), lead)
    return Chapter(
        title=titled.title,
        start_s=min(a.start_s, b.start_s),
        end_s=max(a.end_s, b.end_s),
        kind=lead.kind,
        beats=a.beats + b.beats,
    )


def _merge_overlaps(chapters: list[Chapter]) -> list[Chapter]:
    out: list[Chapter] = []
    for c in sorted(chapters, key=lambda c: (c.start_s, c.end_s)):
        if out and c.start_s <= out[-1].end_s:
            out[-1] = _merge(out[-1], c)
        else:
            out.append(c)
    return out


def _clamp(c: Chapter, game_end_s: float | None) -> Chapter | None:
    """Start >= 0, end <= game_end + 1 s; None (logged) when the chapter starts
    at or past that end, e.g. a beat shifted twice into post-game time."""
    start = round(max(0.0, c.start_s), 2)
    end = c.end_s
    if game_end_s is not None:
        limit = game_end_s + GAME_END_TAIL_S
        if start >= limit:
            _log.warning("story: dropping chapter %r at %.1fs, past game end", c.title, start)
            return None
        end = min(end, limit)
    return c.model_copy(update={"start_s": start, "end_s": round(end, 2)})


def _intensity(c: Chapter, events: list[TimelineEvent]) -> int:
    return sum(
        1
        for e in events
        if e.kind in ("kill", "assist")
        and e.t_rec_s is not None
        and c.start_s <= e.t_rec_s <= c.end_s
    )


def _total(chapters: list[Chapter]) -> float:
    return round(sum(c.end_s - c.start_s for c in chapters), 2)


def _trim(chapters: list[Chapter], events: list[TimelineEvent], max_s: float) -> list[Chapter]:
    kept = list(chapters)
    while _total(kept) > max_s:
        droppable = [c for c in kept if c.kind not in _PROTECTED]
        if not droppable:
            _log.warning("story: lane + close alone run %.0fs > max %.0fs", _total(kept), max_s)
            break
        kept.remove(min(droppable, key=lambda c: (_intensity(c, events), c.end_s - c.start_s)))
    return kept


def plan_story(
    timeline: RiotTimeline,
    beats: list[Beat],
    lane: LaneSummary | None,
    *,
    mic_stream: int | None,
    target_s: float = 330.0,
    max_s: float = 400.0,
    asset_id: str = "",
) -> StoryPlan:
    """Chapters in recording order. `target_s` is advisory: chapters are kept
    while the total stays within `max_s`; when the protected lane + close
    chapters alone exceed it, the plan is returned with `over_budget=True`.
    `mic_stream=0` is refused (stream 0 is the game audio) and falls back to mixed."""
    del target_s  # advisory only; the hard limit is max_s
    if mic_stream == 0:
        _log.warning("story: mic stream 0 is the game audio; using mixed audio")
        mic_stream = None
    fight_list = fights(timeline.events)
    chapters = [_beat_chapter(b, fight_list) for b in beats]
    chapters += _lane_chapters(timeline, lane)
    close = _close_chapter(timeline, fight_list)
    if close is not None:
        chapters.append(close)
    clamped = (_clamp(c, timeline.game_end_s) for c in _merge_overlaps(chapters))
    kept = _trim([c for c in clamped if c is not None], timeline.events, max_s)
    return StoryPlan(
        asset_id=asset_id,
        chapters=kept,
        total_s=_total(kept),
        mic_stream=mic_stream,
        audio="mic_over_game" if mic_stream is not None else "mixed",
        over_budget=_total(kept) > max_s,
    )
