"""Hook picks and the storyboard of a joined video (use-cases behind the routes).

The picks are saved next to the joined video as `<stem>.edits.json`
(`story/hook_edits.py`); the laptop applies them. The joined key IS the stem.
"""

from __future__ import annotations

from pydantic import BaseModel

from ..story.hook_edits import (
    EditHook,
    EditPick,
    HookEdits,
    HookView,
    base_digest,
    chapter_bases,
    chapter_id_of,
    delete_edits,
    fresh_edits,
    hook_view,
    load_edits,
    save_edits,
    validate_picks,
)
from ..story.join_map import JoinMap
from .targets import NarrationTarget


class HookUnavailable(LookupError):  # noqa: N818
    """The video has no join map, or an old one without a hook block."""


class StaleEdits(RuntimeError):  # noqa: N818
    """An edits file made against base windows the laptop has since replaced."""


def _map(target: NarrationTarget, key: str) -> JoinMap:
    jm = target.join_map(key)
    if jm is None or jm.hook is None:
        raise HookUnavailable("no hook data for this video")
    return jm


def get_hook(target: NarrationTarget, key: str) -> HookView:
    jm = _map(target, key)
    return hook_view(jm, key, load_edits(key))


def set_hook_picks(
    target: NarrationTarget,
    key: str,
    picks: list[EditPick],
    *,
    mode: str | None = None,
    title: str | None = None,
) -> HookView:
    jm = _map(target, key)
    assert jm.hook is not None
    digest = base_digest(jm.hook)
    existing = load_edits(key)
    if existing is not None and existing.base_digest != digest:
        raise StaleEdits("the laptop re-joined since these changes: discard or re-apply them")
    prev = existing.hook if existing else None
    new = HookEdits(
        video=key,
        base_digest=digest,
        hook=EditHook(
            mode=mode or (prev.mode if prev else jm.hook.mode),
            title=title if title is not None else (prev.title if prev else jm.hook.title),
            picks=[],
        ),
    )
    new.hook.picks = validate_picks(jm, picks, edits=new)
    if new.hook == fresh_edits(jm, key).hook:
        delete_edits(key)
    else:
        save_edits(new)
    return hook_view(jm, key, load_edits(key))


def reset_hook(target: NarrationTarget, key: str) -> HookView:
    jm = _map(target, key)
    delete_edits(key)
    return hook_view(jm, key, None)


class StoryboardChapter(BaseModel):
    id: str
    title: str
    kind: str
    game_start_s: float
    game_end_s: float
    video_start_s: float
    kill_times_s: list[float]
    death_times_s: list[float]
    in_hook: bool
    has_hook_window: bool


class StoryboardGame(BaseModel):
    round: str
    label: str
    offset_s: float
    chapters: list[StoryboardChapter]


class Storyboard(BaseModel):
    schema_version: int
    video: str
    title: str
    rendered_intro_s: float
    intro_after_s: float
    outro_s: float
    hook: HookView
    games: list[StoryboardGame]


def storyboard(target: NarrationTarget, key: str) -> Storyboard:
    jm = _map(target, key)
    view = hook_view(jm, key, load_edits(key))
    bases = chapter_bases(jm)
    in_hook = {p.chapter_id for p in view.picks}
    games = [
        StoryboardGame(
            round=g.round,
            label=g.label,
            offset_s=g.offset_s,
            chapters=[
                StoryboardChapter(
                    id=(cid := chapter_id_of(g, c)),
                    title=c.title,
                    kind=c.kind,
                    game_start_s=c.game_start_s,
                    game_end_s=c.game_end_s,
                    video_start_s=c.video_start_s,
                    kill_times_s=c.kill_times_s,
                    death_times_s=c.death_times_s,
                    in_hook=cid in in_hook,
                    has_hook_window=cid in bases,
                )
                for c in g.chapters
            ],
        )
        for g in jm.games
    ]
    return Storyboard(
        schema_version=jm.schema_version,
        video=key,
        title=jm.title,
        rendered_intro_s=jm.intro_s,
        intro_after_s=view.intro_after_s,
        outro_s=jm.outro_s,
        hook=view,
        games=games,
    )
