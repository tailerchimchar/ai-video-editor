"""The owner's hook edits: `WORKSPACE/story/<stem>.edits.json`, and the track
maths the web page mirrors.

The hook is a track: each pick is a clip of one chapter placed at `at_s` (the
track position, swipes not counted), with owner offsets `start_s` / `end_s`
(seconds earlier / later than the chapter's base window, in recording seconds).
The PC saves this file; the laptop re-renders from it
(`story-join --apply-edits <stem>`). `base_digest` ties a file to the base
windows it was made against, so a re-planned map shows up as `stale_edits`.

Pure: no HTTP, no ffmpeg. Where a chapter's base window comes from is ONE
question, answered by `chapter_bases(jm)`. For now it only knows the chapters
the map's hook block picked (a split gives the same chapter twice); making it
know every play chapter later is what lets the storyboard add new ones. The
helpers that take a pick's base (`base_len`, `limits`, `clamp_offsets`) accept
a `ChapterBase` (or a `JoinHookPick`, which has the same fields they use).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field, ValidationError

from ..config import settings
from .bookends import FREEZE_S, HOOK_MIN_S
from .join import SWIPE_S
from .join_map import JoinHook, JoinHookEvent, JoinHookPick, JoinMap, chapter_id_from

NEIGHBOUR_GAP_S = 0.5
EDITS_SUFFIX = ".edits.json"
_EPS = 1e-6
_AT_TOL = 0.06  # the map keeps at_s to 0.01, the edits to 0.1
_STEM = re.compile(r"^[A-Za-z0-9_-]+$")


class EditPick(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex[:8])
    chapter_id: str
    at_s: float = 0.0
    start_s: float = 0.0
    end_s: float = 0.0


class EditHook(BaseModel):
    mode: str = "play"
    title: str | None = None
    picks: list[EditPick] = []


class HookEdits(BaseModel):
    schema_version: int = 1
    video: str
    base_digest: str
    hook: EditHook


class ChapterBase(BaseModel):
    """A chapter's base hook window (recording seconds) and what the page needs
    to draw it."""

    chapter_id: str
    round: str
    chapter_index: int
    rec_start_s: float
    rec_end_s: float
    offset_s: float
    chapter_rec_start_s: float
    chapter_rec_end_s: float
    events: list[JoinHookEvent] = []
    label: str


def base_digest(hook: JoinHook) -> str:
    key = json.dumps(sorted((p.chapter_id, p.rec_start_s, p.rec_end_s) for p in hook.picks))
    return "sha256:" + hashlib.sha256(key.encode()).hexdigest()[:12]


def edits_path(video_stem: str) -> Path:
    if not _STEM.fullmatch(video_stem):
        raise ValueError(f"bad video stem: {video_stem!r}")
    return Path(settings.workspace_dir) / "story" / f"{video_stem}{EDITS_SUFFIX}"


def load_edits(video_stem: str) -> HookEdits | None:
    path = edits_path(video_stem)
    if not path.is_file():
        return None
    try:
        return HookEdits.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, ValidationError) as e:
        raise ValueError(f"cannot read {path}: {e}") from e


def save_edits(e: HookEdits) -> Path:
    path = edits_path(e.video)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(e.model_dump(), indent=2))
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


def delete_edits(video_stem: str) -> None:
    edits_path(video_stem).unlink(missing_ok=True)


def _label(title: str) -> str:
    return title.split(",")[0].strip()


def chapter_bases(jm: JoinMap) -> dict[str, ChapterBase]:
    """Every chapter the hook can use, by chapter id: the hook block's picks
    first (they win), then every chapter the map gave a base window."""
    out: dict[str, ChapterBase] = {}
    titles = {(g.round, i): c.title for g in jm.games for i, c in enumerate(g.chapters)}
    for p in jm.hook.picks if jm.hook else []:
        if p.chapter_id in out:
            continue
        out[p.chapter_id] = ChapterBase(
            chapter_id=p.chapter_id,
            round=p.round,
            chapter_index=p.chapter_index,
            rec_start_s=p.rec_start_s,
            rec_end_s=p.rec_end_s,
            offset_s=p.offset_s,
            chapter_rec_start_s=p.chapter_rec_start_s,
            chapter_rec_end_s=p.chapter_rec_end_s,
            events=list(p.events),
            label=_label(titles.get((p.round, p.chapter_index), p.chapter_id)),
        )
    for g in jm.games:
        for i, c in enumerate(g.chapters):
            if c.hook_rec_start_s is None or c.hook_rec_end_s is None:
                continue
            rec_start = round(c.game_start_s + g.offset_s, 3)
            cid = chapter_id_from(g.round, c.kind, c.play_id, rec_start)
            if cid in out:
                continue
            events = [
                JoinHookEvent(rec_s=round(t + g.offset_s, 3), kind="kill") for t in c.kill_times_s
            ]
            events += [
                JoinHookEvent(rec_s=round(t + g.offset_s, 3), kind="death") for t in c.death_times_s
            ]
            out[cid] = ChapterBase(
                chapter_id=cid,
                round=g.round,
                chapter_index=i,
                rec_start_s=c.hook_rec_start_s,
                rec_end_s=c.hook_rec_end_s,
                offset_s=g.offset_s,
                chapter_rec_start_s=rec_start,
                chapter_rec_end_s=round(c.game_end_s + g.offset_s, 3),
                events=sorted(events, key=lambda e: e.rec_s),
                label=_label(c.title),
            )
    return out


def base_len(p: ChapterBase | JoinHookPick, mode: str) -> float:
    """Rendered length of the base window; `tease` clips end on a freeze."""
    return (p.rec_end_s - p.rec_start_s) + (FREEZE_S if mode == "tease" else 0.0)


def _len(b: ChapterBase | JoinHookPick, mode: str, start_s: float, end_s: float) -> float:
    return base_len(b, mode) + start_s + end_s


def limits(jm: JoinMap, p: ChapterBase | JoinHookPick) -> tuple[float, float]:
    """Recording seconds the window may reach: the previous chapter's end + gap
    to the next chapter's start - gap (0 / inf at the ends of the game)."""
    lo, hi = 0.0, math.inf
    for g in jm.games:
        if g.round != p.round:
            continue
        i = p.chapter_index
        if 0 < i < len(g.chapters):
            lo = g.chapters[i - 1].game_end_s + p.offset_s + NEIGHBOUR_GAP_S
        if i + 1 < len(g.chapters):
            hi = g.chapters[i + 1].game_start_s + p.offset_s - NEIGHBOUR_GAP_S
        break
    return lo, hi


def clamp_offsets(
    jm: JoinMap,
    p: ChapterBase | JoinHookPick,
    start_s: float,
    end_s: float,
    keep: Literal["start", "end"],
) -> tuple[float, float]:
    """Offsets whose window stays between the neighbours and is at least
    HOOK_MIN_S long; `keep` is the edge that stays put when it is too short."""
    lo, hi = limits(jm, p)
    start_s = min(start_s, p.rec_start_s - lo)
    end_s = min(end_s, hi - p.rec_end_s)
    window = p.rec_end_s - p.rec_start_s
    if window + start_s + end_s < HOOK_MIN_S:
        if keep == "start":
            end_s = HOOK_MIN_S - window - start_s
        else:
            start_s = HOOK_MIN_S - window - end_s
    # round the edge that moved away from the other one, so rounding never shortens
    if keep == "start":
        return round(start_s, 1), math.ceil(end_s * 10 - 1e-9) / 10
    return math.ceil(start_s * 10 - 1e-9) / 10, round(end_s, 1)


def settle(picks: list[EditPick], lengths: dict[str, float]) -> list[EditPick]:
    """Sorted by `at_s`; a pick that overlaps the previous one is pushed to its
    end; `at_s >= 0`, rounded to 0.1. `lengths` is keyed by pick id."""
    out: list[EditPick] = []
    end = 0.0
    for p in sorted(picks, key=lambda x: x.at_s):
        at = round(max(p.at_s, 0.0), 1)
        if out and at < end - _EPS:
            at = math.ceil(end * 10 - 1e-9) / 10  # up to the grid, so it never overlaps
        if p.id not in lengths:
            raise ValueError(f"length missing for pick id: {p.id}")
        out.append(p.model_copy(update={"at_s": at}))
        end = at + lengths[p.id]
    return out


def _mode_of(jm: JoinMap, edits: HookEdits | None) -> str:
    """The hook mode: the edits' when there is an edits file, else the map's."""
    if edits is not None:
        return edits.hook.mode
    return jm.hook.mode if jm.hook else "play"


def validate_picks(
    jm: JoinMap, picks: list[EditPick], edits: HookEdits | None = None
) -> list[EditPick]:
    """Check chapters and ids, clamp offsets, settle the track."""
    bases = chapter_bases(jm)
    seen: set[str] = set()
    clamped: list[EditPick] = []
    for p in picks:
        if p.chapter_id not in bases:
            raise ValueError(f"unknown chapter_id: {p.chapter_id}")
        if p.id in seen:
            raise ValueError(f"duplicate pick id: {p.id}")
        seen.add(p.id)
        s, e = clamp_offsets(jm, bases[p.chapter_id], p.start_s, p.end_s, "start")
        clamped.append(p.model_copy(update={"start_s": s, "end_s": e}))
    lengths = {
        p.id: _len(bases[p.chapter_id], _mode_of(jm, edits), p.start_s, p.end_s) for p in clamped
    }
    return settle(clamped, lengths)


def split_pick(
    jm: JoinMap,
    picks: list[EditPick],
    pick_id: str,
    at_track_s: float,
    edits: HookEdits | None = None,
) -> list[EditPick]:
    """Cut the pick that holds `at_track_s` into two picks of the same chapter."""
    bases = chapter_bases(jm)
    mode = _mode_of(jm, edits)
    for i, p in enumerate(picks):
        if p.id != pick_id:
            continue
        b = bases.get(p.chapter_id)
        if b is None:
            raise ValueError(f"unknown chapter_id: {p.chapter_id}")
        if not p.at_s <= at_track_s < p.at_s + _len(b, mode, p.start_s, p.end_s):
            raise ValueError("split point is outside the pick")
        rec_t = round(b.rec_start_s - p.start_s + (at_track_s - p.at_s), 1)
        left_win = rec_t - (b.rec_start_s - p.start_s)
        right_win = (b.rec_end_s + p.end_s) - rec_t
        if left_win < HOOK_MIN_S - _EPS or right_win < HOOK_MIN_S - _EPS:
            raise ValueError("too close to the edge to split")
        left = p.model_copy(update={"end_s": round(rec_t - b.rec_end_s, 1)})
        right = EditPick(
            chapter_id=p.chapter_id,
            at_s=round(p.at_s + _len(b, mode, left.start_s, left.end_s), 1),
            start_s=round(b.rec_start_s - rec_t, 1),
            end_s=p.end_s,
        )
        return [*picks[:i], left, right, *picks[i + 1 :]]
    raise ValueError(f"unknown pick id: {pick_id}")


class HookPickView(BaseModel):
    id: str
    chapter_id: str
    round: str
    chapter_index: int
    label: str
    at_s: float
    start_s: float
    end_s: float
    len_s: float
    rec_start_s: float
    rec_end_s: float
    base_rec_start_s: float
    base_rec_end_s: float
    offset_s: float
    events: list[JoinHookEvent]
    chapter_rec_start_s: float
    chapter_rec_end_s: float
    limit_lo_rec_s: float
    limit_hi_rec_s: float | None  # None = no upper limit (JSON has no inf)
    video_start_s: float | None
    video_end_s: float | None
    pending: bool
    beyond_chapter_s: float


class HookView(BaseModel):
    mode: str
    title: str | None
    rendered_intro_s: float
    track_end_s: float
    intro_after_s: float
    picks: list[HookPickView]
    base_digest: str
    edits_digest: str | None
    stale_edits: bool
    dirty: bool
    empty_space_s: float
    laptop_command: str
    edits_file: str


def _same(a: EditPick, r: JoinHookPick) -> bool:
    return (
        a.id == r.id
        and abs(a.at_s - r.at_s) <= _AT_TOL
        and abs(a.start_s - r.start_s) <= _EPS
        and abs(a.end_s - r.end_s) <= _EPS
    )


def fresh_edits(jm: JoinMap, video_stem: str) -> HookEdits:
    """The rendered picks as an edits file."""
    if jm.hook is None:
        raise ValueError("this join has no hook block")
    return HookEdits(
        video=video_stem,
        base_digest=base_digest(jm.hook),
        hook=EditHook(
            mode=jm.hook.mode,
            title=jm.hook.title,
            picks=[
                EditPick(
                    id=p.id,
                    chapter_id=p.chapter_id,
                    at_s=p.at_s,
                    start_s=p.start_s,
                    end_s=p.end_s,
                )
                for p in jm.hook.picks
            ],
        ),
    )


def hook_view(jm: JoinMap, video_stem: str, edits: HookEdits | None) -> HookView:
    """What the page shows: the edited track (or the rendered one) with lengths,
    limits and what still has to be re-rendered."""
    if jm.hook is None:
        raise ValueError("this join has no hook block")
    bases = chapter_bases(jm)
    rendered = {p.id: p for p in jm.hook.picks}
    current = (edits or fresh_edits(jm, video_stem)).hook
    mode = _mode_of(jm, edits)
    ordered = sorted(current.picks, key=lambda x: x.at_s)
    views: list[HookPickView] = []
    gaps = cursor = track_end = total = 0.0
    for p in ordered:
        b = bases.get(p.chapter_id)
        if b is None:  # a stale file may name a chapter the map no longer has
            continue
        ln = _len(b, mode, p.start_s, p.end_s)
        lo, hi = limits(jm, b)
        rec_end = b.rec_end_s + p.end_s
        r = rendered.get(p.id)
        views.append(
            HookPickView(
                id=p.id,
                chapter_id=p.chapter_id,
                round=b.round,
                chapter_index=b.chapter_index,
                label=b.label,
                at_s=p.at_s,
                start_s=p.start_s,
                end_s=p.end_s,
                len_s=ln,
                rec_start_s=b.rec_start_s - p.start_s,
                rec_end_s=rec_end,
                base_rec_start_s=b.rec_start_s,
                base_rec_end_s=b.rec_end_s,
                offset_s=b.offset_s,
                events=b.events,
                chapter_rec_start_s=b.chapter_rec_start_s,
                chapter_rec_end_s=b.chapter_rec_end_s,
                limit_lo_rec_s=lo,
                limit_hi_rec_s=None if math.isinf(hi) else hi,
                video_start_s=r.video_start_s if r else None,
                video_end_s=r.video_end_s if r else None,
                pending=r is None or not _same(p, r),
                beyond_chapter_s=round(max(0.0, rec_end - b.chapter_rec_end_s), 3),
            )
        )
        gaps += max(0.0, p.at_s - cursor)
        cursor = p.at_s + ln
        track_end = max(track_end, cursor)
        total += ln
    digest = base_digest(jm.hook)
    ref = sorted(jm.hook.picks, key=lambda x: x.at_s)
    dirty = (
        current.mode != jm.hook.mode
        or current.title != jm.hook.title
        or len(ordered) != len(ref)
        or any(not _same(a, b) for a, b in zip(ordered, ref, strict=True))
    )
    return HookView(
        mode=mode,
        title=current.title,
        rendered_intro_s=jm.intro_s,
        track_end_s=round(track_end, 3),
        intro_after_s=round(total - len(views) * SWIPE_S, 3) if views else 0.0,
        picks=views,
        base_digest=digest,
        edits_digest=edits.base_digest if edits else None,
        stale_edits=edits is not None and edits.base_digest != digest,
        dirty=dirty,
        empty_space_s=round(gaps, 3),
        laptop_command=f"story-join --apply-edits {video_stem}",
        edits_file=str(edits_path(video_stem)),
    )
