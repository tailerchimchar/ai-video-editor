"""Story planner (pure): chronological chapters from expert beats and the Riot timeline.

Every chapter obeys the fight-window rules from `highlights`: a beat (or lane
event) inside a fight widens to the fight padded by FIGHT_PRE_S
(FIGHT_PRE_ALLY_DEATH_S when the fight opens with an ally or owner death) and
FIGHT_POST_S. A beat chapter whose window holds no owner kill/assist/death is
dropped. Lane chapters are the first two owner kill/death events before 14 min;
the close chapter is the fight containing (or nearest to) the game end and ends
at game_end + 1 s. Overlapping chapters merge; a fight chapter is capped at
FIGHT_CHAPTER_MAX_S, keeping its end. Over `max_s` (title cards included), the
lowest-intensity chapter that is neither lane nor close is dropped first.

Titles: "The close", "Lane vs <champion>" (or "Lane"), else the chapter's payoff
beat (any other beat when no payoff), never a tail / aftermath / cost / "no
event" beat, with its "<label>:" prefix stripped and cut at a word boundary.
"""

from __future__ import annotations

import logging
import re
from typing import Literal

from pydantic import BaseModel, model_validator

from ..highlights import FIGHT_POST_S, FIGHT_PRE_ALLY_DEATH_S, FIGHT_PRE_S
from ..league.lane_models import LaneSummary
from ..league.play_models import GameModel, Play
from ..league.riot_timeline import Fight, RiotTimeline, TimelineEvent, fights

_log = logging.getLogger(__name__)

TITLE_MAX = 40
# The close uses the last fight only when it ends this close to the nexus;
# otherwise the chapter is the end alone. Never longer than STORY_CLOSE_MAX_S.
STORY_CLOSE_REACH_S = 45.0
STORY_CLOSE_MAX_S = 60.0
FIGHT_CHAPTER_MAX_S = 45.0
# Expert beat times are rounded to 0.1 s; Riot events are in ms. A fight this
# close to a beat's edge still counts as overlapping it.
BEAT_TOL_S = 0.5
_LABEL_PREFIX = re.compile(r"^[^:]{1,25}:\s*")
_NO_EVENT = re.compile(r"\bno\s+(?:[\w-]+\s+){0,2}events?\b", re.IGNORECASE)
_EXCLUDED_LABELS = ("tail", "aftermath", "cost")
_TRAILING_WORDS = frozenset({"a", "an", "and", "at", "for", "in", "of", "on", "the", "to", "with"})
FIGHT_TITLE = "The fight"
_EVENT_KINDS = ("kill", "assist", "death")
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
    play_id: str | None = None
    kill_times_s: list[float] = []


class StoryPlan(BaseModel):
    asset_id: str = ""
    chapters: list[Chapter]
    total_s: float
    mic_stream: int | None
    audio: Literal["mic_over_game", "mixed"]
    # True when lane + close chapters alone exceed max_s (nothing left to drop).
    over_budget: bool = False
    # Title cards before each chapter; plays-based plans use dip transitions instead.
    cards: bool = True


def _label(text: str) -> str:
    m = _LABEL_PREFIX.match(text.strip())
    return m.group(0).lower() if m else ""


def clean_title(text: str) -> str:
    """A beat as a card title: drop its "<label>:" prefix, keep the first
    clause, drop a trailing "with <assists>" tail, then cut at a word
    boundary within TITLE_MAX (no dangling "the" / "on")."""
    text = text.strip()
    body = (_LABEL_PREFIX.sub("", text) or text).split(";")[0].strip()
    if len(body) > TITLE_MAX:
        body = re.split(r"\s+with\s+", body, maxsplit=1)[0]
    if len(body) > TITLE_MAX:
        cut = body[: TITLE_MAX + 1]
        body = cut.rsplit(" ", 1)[0] if " " in cut else body[:TITLE_MAX]
    words = body.rstrip(" ,.:-").split(" ")
    while len(words) > 1 and words[-1].lower() in _TRAILING_WORDS:
        words.pop()
    return " ".join(words).rstrip(" ,.:-")


def _title_rank(beat: str) -> int | None:
    """0 = payoff, 1 = any other usable beat, None = never a title."""
    label = _label(beat)
    if any(word in label for word in _EXCLUDED_LABELS) or _NO_EVENT.search(beat):
        return None
    return 0 if "payoff" in label else 1


def _chapter_title(c: Chapter, lane: LaneSummary | None) -> str:
    if c.kind == "close":
        return CLOSE_TITLE
    if c.kind == "lane":
        return f"Lane vs {lane.opponent_champion}" if lane else "Lane"
    ranked = [(r, i, b) for i, b in enumerate(c.beats) if (r := _title_rank(b)) is not None]
    return clean_title(min(ranked)[2]) if ranked else FIGHT_TITLE


def _opens_with_death(events: list[TimelineEvent]) -> bool:
    return bool(events) and events[0].kind in ("ally_death", "death")


def _pre(events: list[TimelineEvent]) -> float:
    return FIGHT_PRE_ALLY_DEATH_S if _opens_with_death(events) else FIGHT_PRE_S


def _fight_window(f: Fight) -> tuple[float, float]:
    return f.start_s - _pre(f.events), f.end_s + FIGHT_POST_S


def _overlapping_fights(fight_list: list[Fight], start: float, end: float) -> list[Fight]:
    return [f for f in fight_list if f.start_s <= end and f.end_s >= start]


def _widened(start: float, end: float, fight_list: list[Fight]) -> tuple[float, float]:
    """Union of [start, end] and the padded window of every fight it overlaps."""
    windows = [(start, end)]
    near = _overlapping_fights(fight_list, start - BEAT_TOL_S, end + BEAT_TOL_S)
    windows += [_fight_window(f) for f in near]
    return min(w[0] for w in windows), max(w[1] for w in windows)


def _beat_chapter(b: Beat, fight_list: list[Fight]) -> Chapter:
    start, end = _widened(b.t_start, b.t_end, fight_list)
    kind = "fight" if b.kind == "close" else b.kind
    return Chapter(title="", start_s=start, end_s=end, kind=kind, beats=[b.beat])


def _has_event(c: Chapter, events: list[TimelineEvent]) -> bool:
    return any(
        e.kind in _EVENT_KINDS and e.t_rec_s is not None and c.start_s <= e.t_rec_s <= c.end_s
        for e in events
    )


def _lane_chapters(tl: RiotTimeline, fight_list: list[Fight]) -> list[Chapter]:
    owner = [
        e
        for e in tl.events
        if e.kind in ("kill", "death") and e.t_rec_s is not None and e.t_game_s < LANE_BEFORE_S
    ]
    out = []
    for e in sorted(owner, key=lambda e: e.t_rec_s)[:LANE_EVENTS]:
        pre = FIGHT_PRE_ALLY_DEATH_S if e.kind == "death" else FIGHT_PRE_S
        start, end = _widened(e.t_rec_s - pre, e.t_rec_s + FIGHT_POST_S, fight_list)
        out.append(Chapter(title="", start_s=start, end_s=end, kind="lane"))
    return out


def _cap_fight(c: Chapter) -> Chapter:
    """A fight chapter runs at most FIGHT_CHAPTER_MAX_S: keep the payoff end."""
    if c.kind != "fight" or c.end_s - c.start_s <= FIGHT_CHAPTER_MAX_S:
        return c
    return c.model_copy(update={"start_s": round(c.end_s - FIGHT_CHAPTER_MAX_S, 2)})


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
    return Chapter(
        title="",
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
            _log.warning("story: dropping chapter %r at %.1fs, past game end", c.beats, start)
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


def _total(chapters: list[Chapter], card_s: float = 0.0) -> float:
    return round(sum(c.end_s - c.start_s + card_s for c in chapters), 2)


def _trim(
    chapters: list[Chapter], events: list[TimelineEvent], max_s: float, card_s: float
) -> list[Chapter]:
    kept = list(chapters)
    while _total(kept, card_s) > max_s:
        droppable = [c for c in kept if c.kind not in _PROTECTED]
        if not droppable:
            total = _total(kept, card_s)
            _log.warning("story: lane + close alone run %.0fs > max %.0fs", total, max_s)
            break
        kept.remove(min(droppable, key=lambda c: (_intensity(c, events), c.end_s - c.start_s)))
    return kept


def plan_story(
    timeline: RiotTimeline,
    beats: list[Beat],
    lane: LaneSummary | None,
    *,
    mic_stream: int | None,
    max_s: float = 360.0,
    card_s: float = 0.0,
    asset_id: str = "",
) -> StoryPlan:
    """Chapters in recording order, kept while the total (chapters plus one
    `card_s` title card per chapter; 0 when cards are off) stays within
    `max_s`. When the protected lane + close chapters alone exceed it, the
    plan is returned with `over_budget=True`. `mic_stream=0` is refused
    (stream 0 is the game audio) and falls back to mixed."""
    if mic_stream == 0:
        _log.warning("story: mic stream 0 is the game audio; using mixed audio")
        mic_stream = None
    fight_list = fights(timeline.events)
    chapters = []
    for b in beats:
        c = _beat_chapter(b, fight_list)
        if _has_event(c, timeline.events):
            chapters.append(c)
        else:
            _log.info("story: beat %r holds no Riot kill/assist/death; dropped", b.beat)
    chapters += _lane_chapters(timeline, fight_list)
    close = _close_chapter(timeline, fight_list)
    if close is not None:
        chapters.append(close)
    merged = (_cap_fight(c) for c in _merge_overlaps(chapters))
    clamped = (_clamp(c, timeline.game_end_s) for c in merged)
    titled = [
        c.model_copy(update={"title": _chapter_title(c, lane)}) for c in clamped if c is not None
    ]
    kept = _trim(titled, timeline.events, max_s, card_s)
    total = _total(kept, card_s)
    return StoryPlan(
        asset_id=asset_id,
        chapters=kept,
        total_s=total,
        mic_stream=mic_stream,
        audio="mic_over_game" if mic_stream is not None else "mixed",
        over_budget=total > max_s,
    )


PLAY_PRE_FALLBACK_S = 8.0
PLAY_POST_FALLBACK_S = 3.0
_UNRANKED = 10**6


def _play_kind(p: Play) -> str:
    if p.phase == "early":
        return "lane"
    return "close" if p.type == "closeout" else "fight"


def _play_chapter(p: Play, timeline: RiotTimeline) -> Chapter | None:
    w = p.window
    start = w.approach_s if w.approach_s is not None else p.recording_time.first_kill_s - 8.0
    end = w.outcome_s if w.outcome_s is not None else p.recording_time.last_kill_s + 3.0
    start = max(0.0, start)
    if timeline.game_end_s is not None:
        end = min(end, timeline.game_end_s + GAME_END_TAIL_S)
    if end <= start:
        _log.warning("story: dropping play %s, empty window after clamp", p.id)
        return None
    shift = w.offset_correction_s or 0.0  # banner-measured Riot offset error (D17)
    kills = [
        e.t_rec_s + shift
        for e in timeline.events
        if e.kind == "kill" and e.t_rec_s is not None and start <= e.t_rec_s + shift <= end
    ]
    return Chapter(
        title=p.text,
        start_s=round(start, 2),
        end_s=round(end, 2),
        kind=_play_kind(p),
        play_id=p.id,
        kill_times_s=kills,
    )


def plan_story_from_plays(
    game: GameModel,
    timeline: RiotTimeline,
    *,
    mic_stream: int | None,
    max_s: float = 360.0,
    asset_id: str = "",
) -> StoryPlan:
    """One chapter per owner-involved play (no merging, no cap, no trimming),
    in recording order. Over `max_s` whole chapters are dropped, least
    impressive first (never a close chapter); `over_budget` when the close
    alone exceeds it. Plans carry `cards=False` (dip transitions instead)."""
    if mic_stream == 0:
        _log.warning("story: mic stream 0 is the game audio; using mixed audio")
        mic_stream = None
    rank_of = {p.id: p.impressive.rank for p in game.plays}
    chapters = [
        c for p in game.plays if p.owner.involved and (c := _play_chapter(p, timeline)) is not None
    ]
    chapters.sort(key=lambda c: c.start_s)
    kept = list(chapters)
    while _total(kept) > max_s:
        droppable = [c for c in kept if c.kind != "close"]
        if not droppable:
            _log.warning("story: close alone runs %.0fs > max %.0fs", _total(kept), max_s)
            break
        worst = max(droppable, key=lambda c: rank_of.get(c.play_id) or _UNRANKED)
        kept.remove(worst)
    total = _total(kept)
    return StoryPlan(
        asset_id=asset_id,
        chapters=kept,
        total_s=total,
        mic_stream=mic_stream,
        audio="mic_over_game" if mic_stream is not None else "mixed",
        over_budget=total > max_s,
        cards=False,
    )
