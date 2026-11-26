"""Owner story requests: `WORKSPACE/story/<round>/requests.json`.

The owner's asks ("show me walking out of base at 20:05", "make the last
teamfight cinematic") are applied on top of the automatic plan every time the
story renders. Times are the game clock the owner sees ("20:05", "1:02:05" or
seconds as a number); recording seconds = game clock + `timeline.offset_s`.

Kinds: `add_chapter`, `extend`, `drop`, `trim` (shrink the chapter at `at`
to the game-clock `start`..`end`), `mute` (silence only the owner's mic over
`start`..`end`, default the whole chapter; game audio keeps playing) and
`effect`.

`apply_requests` is pure. Order: drops, adds, extends, trims, mutes, effects. Request
chapters are applied after budgeting, so no budget ever drops them.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

from ..league.riot_timeline import RiotTimeline
from .planner import GAME_END_TAIL_S, Chapter, ChapterEffect, EffectKind, StoryPlan

REQUESTS_FILE = "requests.json"
_TIME_EPS = 1e-6

Clock = str | float


def game_clock_s(v: str | float) -> float:
    """ "20:05" -> 1205, "1:02:05" -> 3725, a number (or numeric string) as is."""
    if isinstance(v, bool) or not isinstance(v, (str, int, float)):
        raise ValueError(f"not a game clock: {v!r}")
    if isinstance(v, (int, float)):
        out = float(v)
    else:
        parts = v.strip().split(":")
        if not 1 <= len(parts) <= 3:
            raise ValueError(f"not a game clock: {v!r}")
        try:
            nums = [float(p) for p in parts]
        except ValueError:
            raise ValueError(f"not a game clock: {v!r}") from None
        if any(n < 0 for n in nums) or any(n >= 60 for n in nums[1:]):
            raise ValueError(f"not a game clock: {v!r}")
        out = 0.0
        for n in nums:
            out = out * 60 + n
    if out != out or out < 0:  # NaN or negative
        raise ValueError(f"not a game clock: {v!r}")
    return out


def _checked(v):
    game_clock_s(v)
    return v


class _Timed(BaseModel):
    at: Clock

    @field_validator("at", mode="after")
    @classmethod
    def _clock(cls, v):
        return _checked(v)


class AddChapter(_Timed):
    kind: Literal["add_chapter"] = "add_chapter"
    before_s: float = 5.0
    after_s: float = 20.0
    title: str | None = None
    note: str | None = None


class Extend(_Timed):
    kind: Literal["extend"] = "extend"
    before_s: float = 0.0
    after_s: float = 0.0


class Drop(_Timed):
    kind: Literal["drop"] = "drop"


class _Range(_Timed):
    start: Clock | None = None
    end: Clock | None = None

    @field_validator("start", "end", mode="after")
    @classmethod
    def _opt_clock(cls, v):
        return v if v is None else _checked(v)


class Trim(_Range):
    kind: Literal["trim"] = "trim"


class Mute(_Range):
    kind: Literal["mute"] = "mute"


class Effect(_Timed):
    kind: Literal["effect"] = "effect"
    effect: EffectKind
    start: Clock | None = None
    end: Clock | None = None
    params: dict[str, float | str] = {}

    @field_validator("start", "end", mode="after")
    @classmethod
    def _opt_clock(cls, v):
        return v if v is None else _checked(v)


OwnerRequest = Annotated[
    AddChapter | Extend | Drop | Trim | Mute | Effect, Field(discriminator="kind")
]


class StoryRequests(BaseModel):
    requests: list[OwnerRequest] = []


def load_requests(story_dir: Path) -> StoryRequests:
    """Missing file -> empty. Invalid -> ValueError naming the bad entry."""
    path = Path(story_dir) / REQUESTS_FILE
    if not path.is_file():
        return StoryRequests()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ValueError(f"{REQUESTS_FILE}: unreadable: {e}") from e
    entries = raw.get("requests") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        raise ValueError(f'{REQUESTS_FILE}: expected {{"requests": [...]}}')
    parsed = []
    for i, entry in enumerate(entries, start=1):
        try:
            parsed.append(StoryRequests.model_validate({"requests": [entry]}).requests[0])
        except ValidationError as e:
            first = e.errors()[0]
            where = ".".join(str(p) for p in first["loc"][2:]) or first["type"]
            raise ValueError(
                f"{REQUESTS_FILE}: request #{i} invalid ({where}): {first['msg']}"
            ) from e
    return StoryRequests(requests=parsed)


def save_requests(story_dir: Path, reqs: StoryRequests) -> Path:
    path = Path(story_dir) / REQUESTS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(reqs.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8")
    return path


# ---- applying ---------------------------------------------------------------


def _rec(v: Clock, timeline: RiotTimeline) -> float:
    return game_clock_s(v) + timeline.offset_s


def _containing(chapters: list[Chapter], t: float) -> int | None:
    for i, c in enumerate(chapters):
        if c.start_s - _TIME_EPS <= t <= c.end_s + _TIME_EPS:
            return i
    return None


def _event_times(timeline: RiotTimeline, kind: str, start: float, end: float) -> list[float]:
    return sorted(
        e.t_rec_s
        for e in timeline.events
        if e.kind == kind and e.t_rec_s is not None and start <= e.t_rec_s <= end
    )


def _refill(c: Chapter, timeline: RiotTimeline, covered: list[tuple[float, float]]) -> Chapter:
    """Kill/death times for a new or grown chapter: the times it already had
    plus every timeline event outside the windows it already covered."""

    def fill(existing: list[float], kind: str) -> list[float]:
        new = [
            t
            for t in _event_times(timeline, kind, c.start_s, c.end_s)
            if not any(lo <= t <= hi for lo, hi in covered)
        ]
        return sorted(set(existing) | set(new))

    return c.model_copy(
        update={
            "kill_times_s": fill(c.kill_times_s, "kill"),
            "death_times_s": fill(c.death_times_s, "death"),
        }
    )


def _union(a: Chapter, b: Chapter) -> Chapter:
    """a (the earlier / existing chapter) keeps its title, kind and play."""
    return a.model_copy(
        update={
            "start_s": min(a.start_s, b.start_s),
            "end_s": max(a.end_s, b.end_s),
            "beats": a.beats + [x for x in b.beats if x not in a.beats],
            "kill_times_s": sorted(set(a.kill_times_s) | set(b.kill_times_s)),
            "death_times_s": sorted(set(a.death_times_s) | set(b.death_times_s)),
            "broll": a.broll + b.broll,
            "effects": a.effects + b.effects,
            "mic_mutes": a.mic_mutes + b.mic_mutes,
        }
    )


# (chapter, windows it already covered, whether its times need refilling)
_Entry = tuple[Chapter, list[tuple[float, float]], bool]


def _settle(entries: list[_Entry], timeline: RiotTimeline) -> list[Chapter]:
    """Chronological and overlap-free (overlapping chapters merge, the earlier
    one keeping title and kind); new or grown chapters get kill/death times
    refilled."""
    out: list[_Entry] = []
    for c, cov, dirty in sorted(entries, key=lambda e: (e[0].start_s, e[0].end_s)):
        if out and c.start_s < out[-1][0].end_s:
            pc, pcov, pdirty = out[-1]
            out[-1] = (_union(pc, c), pcov + cov, pdirty or dirty)
        else:
            out.append((c, cov, dirty))
    return [_refill(c, timeline, cov) if dirty else c for c, cov, dirty in out]


def _keep(chapters: list[Chapter]) -> list[_Entry]:
    return [(c, [(c.start_s, c.end_s)], False) for c in chapters]


def _card_s(plan: StoryPlan) -> float:
    if not plan.chapters:
        return 0.0
    dur = sum(c.end_s - c.start_s for c in plan.chapters)
    return max(0.0, (plan.total_s - dur) / len(plan.chapters))


def _clip_ranges(
    ranges: list[tuple[float, float]], start: float, end: float
) -> list[tuple[float, float]]:
    out = [(max(a, start), min(b, end)) for a, b in ranges]
    return [(a, b) for a, b in out if b > a]


def _trimmed(c: Chapter, start: float, end: float) -> dict:
    """Chapter fields for the window [start, end] inside it: events, effects,
    mutes and b-roll outside the window are dropped or clamped."""
    effects = [
        e.model_copy(update={"start_s": max(e.start_s, start), "end_s": min(e.end_s, end)})
        for e in c.effects
    ]
    return {
        "start_s": start,
        "end_s": end,
        "kill_times_s": [t for t in c.kill_times_s if start <= t <= end],
        "death_times_s": [t for t in c.death_times_s if start <= t <= end],
        "effects": [e for e in effects if e.end_s > e.start_s],
        "mic_mutes": _clip_ranges(c.mic_mutes, start, end),
        "broll": [b for b in c.broll if b.mic_start_s >= start and b.mic_end_s <= end],
    }


def _label(req) -> str:
    return f"{req.kind} at {req.at}"


def apply_requests(
    plan: StoryPlan, reqs: StoryRequests, timeline: RiotTimeline
) -> tuple[StoryPlan, list[str]]:
    """The plan with the owner's requests applied, plus a warning for every
    request that matched no chapter. Pure."""
    warnings: list[str] = []
    if not reqs.requests:
        return plan, warnings
    chapters = [c.model_copy(deep=True) for c in plan.chapters]
    limit = None if timeline.game_end_s is None else timeline.game_end_s + GAME_END_TAIL_S

    def clamp(start: float, end: float) -> tuple[float, float]:
        start = round(max(0.0, start), 2)
        end = round(end if limit is None else min(end, limit), 2)
        return start, end

    for r in (r for r in reqs.requests if isinstance(r, Drop)):
        i = _containing(chapters, _rec(r.at, timeline))
        if i is None:
            warnings.append(f"{_label(r)}: no chapter there to drop")
        else:
            chapters.pop(i)

    for r in (r for r in reqs.requests if isinstance(r, AddChapter)):
        t = _rec(r.at, timeline)
        start, end = clamp(t - r.before_s, t + r.after_s)
        if end <= start:
            warnings.append(f"{_label(r)}: window is empty after clamping")
            continue
        new = Chapter(
            title=r.title or f"At {r.at}",
            start_s=start,
            end_s=end,
            kind="request",
            beats=[r.note] if r.note else [],
        )
        chapters = _settle([*_keep(chapters), (new, [], True)], timeline)

    for r in (r for r in reqs.requests if isinstance(r, Extend)):
        i = _containing(chapters, _rec(r.at, timeline))
        if i is None:
            warnings.append(f"{_label(r)}: no chapter there to extend")
            continue
        c = chapters[i]
        start, end = clamp(c.start_s - r.before_s, c.end_s + r.after_s)
        new = c.model_copy(update={"start_s": min(start, c.start_s), "end_s": max(end, c.end_s)})
        entries = _keep(chapters)
        entries[i] = (new, [(c.start_s, c.end_s)], True)
        chapters = _settle(entries, timeline)

    for r in (r for r in reqs.requests if isinstance(r, Trim)):
        i = _containing(chapters, _rec(r.at, timeline))
        if i is None:
            warnings.append(f"{_label(r)}: no chapter there to trim")
            continue
        c = chapters[i]
        start = c.start_s if r.start is None else max(c.start_s, _rec(r.start, timeline))
        end = c.end_s if r.end is None else min(c.end_s, _rec(r.end, timeline))
        if end <= start:
            warnings.append(f"{_label(r)}: trim window is empty")
            continue
        chapters[i] = c.model_copy(update=_trimmed(c, round(start, 2), round(end, 2)))

    for r in (r for r in reqs.requests if isinstance(r, Mute)):
        i = _containing(chapters, _rec(r.at, timeline))
        if i is None:
            warnings.append(f"{_label(r)}: no chapter there to mute")
            continue
        c = chapters[i]
        start = c.start_s if r.start is None else max(c.start_s, _rec(r.start, timeline))
        end = c.end_s if r.end is None else min(c.end_s, _rec(r.end, timeline))
        if end <= start:
            warnings.append(f"{_label(r)}: mute range falls outside its chapter")
            continue
        mute = (round(start, 2), round(end, 2))
        chapters[i] = c.model_copy(update={"mic_mutes": [*c.mic_mutes, mute]})

    for r in (r for r in reqs.requests if isinstance(r, Effect)):
        i = _containing(chapters, _rec(r.at, timeline))
        if i is None:
            warnings.append(f"{_label(r)}: no chapter there for the {r.effect} effect")
            continue
        c = chapters[i]
        start = c.start_s if r.start is None else max(c.start_s, _rec(r.start, timeline))
        end = c.end_s if r.end is None else min(c.end_s, _rec(r.end, timeline))
        if end <= start:
            warnings.append(f"{_label(r)}: {r.effect} range falls outside its chapter")
            continue
        fx = ChapterEffect(
            kind=r.effect, start_s=round(start, 2), end_s=round(end, 2), params=dict(r.params)
        )
        chapters[i] = c.model_copy(update={"effects": [*c.effects, fx]})

    card = _card_s(plan)
    total = round(sum(c.end_s - c.start_s + card for c in chapters), 2)
    return plan.model_copy(update={"chapters": chapters, "total_s": total}), warnings
