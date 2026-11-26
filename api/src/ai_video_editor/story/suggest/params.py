"""Default span and params of each suggested effect (game-clock seconds).

letterbox_focus and zoom cover the whole candidate; slow-mo covers at most
SLOWMO_MAX_S around the owner's kill (the candidate's middle without one).
Param names are the renderer's (`planner.ChapterEffect`)."""

from __future__ import annotations

from .models import Candidate, EffectChoice

SLOWMO_MAX_S = 2.5
SLOWMO_PRE_S = 1.0

DEFAULT_PARAMS: dict[EffectChoice, dict[str, float | str]] = {
    EffectChoice.letterbox_focus: {"bar": 0.12},  # the focus half takes no params
    EffectChoice.slowmo: {"factor": 0.5},
    EffectChoice.zoom: {"factor": 1.3, "roi": "center"},
}


def _slowmo_window(c: Candidate) -> tuple[float, float]:
    anchor = c.anchor_s if c.anchor_s is not None else (c.start_s + c.end_s) / 2
    start, end = anchor - SLOWMO_PRE_S, anchor - SLOWMO_PRE_S + SLOWMO_MAX_S
    if end > c.end_s:
        start, end = start - (end - c.end_s), c.end_s
    if start < c.start_s:
        start, end = c.start_s, end + (c.start_s - start)
    return round(start, 2), round(min(end, c.end_s), 2)


def effect_span(effect: EffectChoice, c: Candidate) -> tuple[float, float, dict[str, float | str]]:
    params = dict(DEFAULT_PARAMS.get(effect, {}))
    if effect is EffectChoice.slowmo:
        return (*_slowmo_window(c), params)
    return c.start_s, c.end_s, params
