"""`story-join --apply-edits <stem>`: re-join from the owner's hook edits.

The PC narrate page writes `WORKSPACE/story/<stem>.edits.json` (`hook_edits`).
Here (the laptop) every pick's window is checked against its chapter's footage;
a window that runs past it becomes ONE managed `Extend` request
(`source="clip_edit"`) in that round's `requests.json`, the round re-renders
once, and the join runs with the picks in track order. The edits file is
deleted after a successful join.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from ..config import settings
from .bookends import HookPick
from .hook_edits import (
    EditPick,
    base_digest,
    chapter_bases,
    delete_edits,
    load_edits,
)
from .join_map import JoinMap, chapter_id
from .planner import Chapter, StoryPlan
from .render import StoryRenderError
from .requests import Extend, game_clock_s, load_requests, save_requests

CLIP_EDIT = "clip_edit"
_START_TOL_S = 1.0


def _load_map(stem: str) -> JoinMap:
    path = Path(settings.workspace_dir) / "story" / f"{stem}.join.json"
    if not path.is_file():
        raise StoryRenderError(f"no join map for {stem}: {path}")
    jm = JoinMap.model_validate_json(path.read_text(encoding="utf-8"))
    if jm.schema_version < 2 or jm.hook is None:
        raise StoryRenderError(f"{stem} has no hook block (join map version {jm.schema_version})")
    return jm


def _resolve(plan: StoryPlan, round_name: str, cid: str, base_start_s: float) -> int | None:
    for i, c in enumerate(plan.chapters):
        if chapter_id(round_name, c) == cid:
            return i
    for i, c in enumerate(plan.chapters):
        if abs(c.start_s - base_start_s) <= _START_TOL_S:
            return i
    return None


def _managed(reqs, ch: Chapter, offset: float) -> list[Extend]:
    return [
        r
        for r in reqs.requests
        if isinstance(r, Extend)
        and r.source == CLIP_EDIT
        and ch.start_s <= game_clock_s(r.at) + offset <= ch.end_s
    ]


def apply_edits(
    video_stem: str,
    *,
    force: bool = False,
    say: Callable[[str], None] = print,
    render=None,
    join=None,
) -> Path:
    from .cli import render_round
    from .join import _load_plan, default_out, join_stories

    render = render or render_round
    join = join or join_stories

    jm = _load_map(video_stem)
    assert jm.hook is not None
    edits = load_edits(video_stem)
    if edits is None:
        raise StoryRenderError("no edits file")
    digest = base_digest(jm.hook)
    if edits.base_digest != digest and not force:
        raise StoryRenderError(
            f"edits were made on {edits.base_digest}, the map is {digest}; "
            "re-join first or pass --force-edits"
        )

    rounds = [g.round for g in jm.games]
    offsets = {g.round: g.offset_s for g in jm.games}
    bases = chapter_bases(jm)
    plans: dict[str, StoryPlan] = {}
    # (round, chapter index in the current plan) -> wanted recording window
    wanted: dict[tuple[str, int], tuple[float, float]] = {}
    resolved: list[tuple[EditPick, str, int]] = []
    for p in sorted(edits.hook.picks, key=lambda x: x.at_s):
        b = bases.get(p.chapter_id)
        if b is None or b.round not in rounds:
            say(f"skipped {p.chapter_id}: no such chapter in {b.round if b else '?'} any more")
            continue
        plan = plans.setdefault(b.round, _load_plan(b.round))
        idx = _resolve(plan, b.round, p.chapter_id, b.chapter_rec_start_s)
        if idx is None:
            say(f"skipped {p.chapter_id}: no such chapter in {b.round} any more")
            continue
        resolved.append((p, b.round, idx))
        lo, hi = b.rec_start_s - p.start_s, b.rec_end_s + p.end_s
        old = wanted.get((b.round, idx))
        wanted[(b.round, idx)] = (lo, hi) if old is None else (min(old[0], lo), max(old[1], hi))

    touched: list[str] = []
    by_round: dict[str, list[int]] = {}
    for rnd, idx in wanted:
        by_round.setdefault(rnd, []).append(idx)
    for rnd, idxs in by_round.items():
        story_dir = Path(settings.workspace_dir) / "story" / rnd
        reqs = load_requests(story_dir)
        before = reqs.model_dump(mode="json")
        off = offsets[rnd]
        for idx in idxs:
            ch = plans[rnd].chapters[idx]
            old = _managed(reqs, ch, off)
            reqs.requests = [r for r in reqs.requests if r not in old]
            # the chapter as it was before our managed extend(s)
            base_s = ch.start_s + sum(r.before_s for r in old)
            base_e = ch.end_s - sum(r.after_s for r in old)
            lo, hi = wanted[(rnd, idx)]
            before_s, after_s = max(0.0, base_s - lo), max(0.0, hi - base_e)
            if before_s > 0 or after_s > 0:
                mid = (base_s + base_e) / 2 - off
                reqs.requests.append(
                    Extend(
                        at=round(max(mid, 0.0), 3),
                        before_s=round(before_s, 3),
                        after_s=round(after_s, 3),
                        source=CLIP_EDIT,
                    )
                )
        if reqs.model_dump(mode="json") != before:
            save_requests(story_dir, reqs)
            touched.append(rnd)
    for rnd in touched:
        say(f"re-rendering {rnd} for the extended clips")
        render(asset_id=plans[rnd].asset_id, round=rnd)
        plans[rnd] = _load_plan(rnd)

    picks = [
        HookPick(
            game=rounds.index(rnd) + 1,
            chapter=idx + 1,
            text=next((x.text for x in jm.hook.picks if x.id == p.id), ""),
            mode=edits.hook.mode,  # type: ignore[arg-type]
            start_s=p.start_s,
            end_s=p.end_s,
            id=p.id,
        )
        for p, rnd, idx in resolved
    ]
    out = join(
        rounds,
        default_out(rounds),
        subscribe_after=jm.options.subscribe_after,
        title=jm.title,
        hook=picks,
        hook_title=edits.hook.title if edits.hook.title is not None else jm.hook.title,
        outro=jm.options.outro,
        music_start_s=jm.options.music_start_s,
    )
    delete_edits(video_stem)
    return Path(out)
