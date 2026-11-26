"""Where a recording second lands in the rendered `story.mp4`.

Mirrors how `story.render.render_story` lays the video out: per chapter an
optional title card, then the chapter footage, where slow-mo effects stretch
time (`effect_time_map`) and everything else plays 1:1. Pure: no ffprobe.
"""

from __future__ import annotations

from ..planner import Chapter, StoryPlan
from ..render import effect_time_map


def _footage_s(ch: Chapter) -> float:
    # Broll wins over effects in the renderer, and broll keeps the length.
    if ch.effects and not ch.broll:
        return effect_time_map(ch.effects, ch.start_s, ch.end_s)(ch.end_s)
    return ch.end_s - ch.start_s


def chapter_footage_starts(plan: StoryPlan, card_s: float) -> list[float]:
    """Video second where each chapter's footage begins (after its card)."""
    card = card_s if plan.cards else 0.0
    starts: list[float] = []
    t = 0.0
    for ch in plan.chapters:
        t += card
        starts.append(round(t, 3))
        t += _footage_s(ch)
    return starts


def video_second(plan: StoryPlan, chapter_index: int, rec_s: float, card_s: float) -> float | None:
    """Video second of recording second `rec_s` inside chapter `chapter_index`;
    None when the index is out of range."""
    if not 0 <= chapter_index < len(plan.chapters):
        return None
    ch = plan.chapters[chapter_index]
    rec = min(max(rec_s, ch.start_s), ch.end_s)
    inside = (
        effect_time_map(ch.effects, ch.start_s, ch.end_s)(rec)
        if (ch.effects and not ch.broll)
        else rec - ch.start_s
    )
    return round(chapter_footage_starts(plan, card_s)[chapter_index] + inside, 2)
