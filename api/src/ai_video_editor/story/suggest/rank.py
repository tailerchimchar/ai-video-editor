"""Mentor's cut (pure): turn scored candidates into at most MAX_ITEMS
suggestions.

Order by play score x viewer "pop" probability (0.5 when the viewer had no
opinion); drop `none`; keep at most one letterbox_focus (the best-ranked:
in practice the last or biggest fight); no two suggestions overlap; every
span is clamped inside its chapter (dropped when nothing is left). When the
panel picks fewer than MIN_ITEMS, the best-ranked remaining candidates are
topped up (`topup_effect`); fewer is fine only when those run out too.
"""

from __future__ import annotations

from .models import Candidate, EffectChoice, SuggestionItem
from .params import effect_span
from .verdict import Verdict

MIN_ITEMS = 3
MAX_ITEMS = 6
NO_OPINION_POP = 0.5

Scored = tuple[Candidate, Verdict]


def _p(value: float) -> str:
    return f"p {value:.2f}".replace("p 0.", "p .")


def _lead(facts: dict) -> str:
    if facts.get("outnumbered_win") and facts.get("numbers"):
        return f"{facts['numbers']} won"
    if (facts.get("multikill") or 0) >= 2:
        return f"{facts['multikill']} kills in a row"
    if facts.get("is_last_fight"):
        return "last fight"
    if facts.get("objective"):
        return f"{facts['objective']} taken"
    return f"{facts.get('kills', 0)} kill(s), {facts.get('deaths', 0)} death(s)"


def reason(c: Candidate, v: Verdict) -> str:
    """e.g. "4v5 won · play 5/5 (p .82) · pop (p .91) · slowmo (p .60)"."""
    play = f"play {v.score}/5 " + (f"({_p(v.score_p)})" if v.score_p is not None else "(default)")
    pop = "pop " + (f"({_p(v.pop_p)})" if v.pop_p is not None else "(no opinion)")
    edit = f"{v.effect.value} " + (f"({_p(v.effect_p)})" if v.effect_p is not None else "(default)")
    parts = [_lead(c.facts), play, pop, edit]
    if c.facts.get("is_last_fight") and parts[0] != "last fight":
        parts.insert(1, "last fight")
    return " · ".join(parts)


def _value(v: Verdict) -> float:
    return v.score * (v.pop_p if v.pop_p is not None else NO_OPINION_POP)


def _overlaps(a: tuple[float, float], b: tuple[float, float]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def topup_effect(c: Candidate) -> EffectChoice:
    """The effect a top-up pick gets: the last fight goes cinematic, a fight
    with an owner kill gets slow-mo on that kill; anything else is skipped."""
    if c.facts.get("is_last_fight"):
        return EffectChoice.letterbox_focus
    if c.anchor_s is not None:
        return EffectChoice.slowmo
    return EffectChoice.none


def _try_add(
    kept: list[SuggestionItem],
    c: Candidate,
    v: Verdict,
    effect: EffectChoice,
    chapter_spans: dict[int, tuple[float, float]],
    why: str,
) -> bool:
    if effect is EffectChoice.none:
        return False
    if effect is EffectChoice.letterbox_focus and any(
        i.kind == EffectChoice.letterbox_focus.value for i in kept
    ):
        return False
    start, end, params = effect_span(effect, c)
    lo, hi = chapter_spans.get(c.chapter_index, (start, end))
    start, end = max(start, lo), min(end, hi)
    if end <= start or any(_overlaps((start, end), (i.start_s, i.end_s)) for i in kept):
        return False
    kept.append(
        SuggestionItem(
            id=f"s-{c.id}-{effect.value}",
            kind=effect.value,
            chapter_index=c.chapter_index,
            start_s=start,
            end_s=end,
            params=params,
            reason=why,
            play_score=v.score if v.score_p is not None else None,
            pop_p=v.pop_p,
            backend=v.backend,
        )
    )
    return True


def rank(
    scored: list[Scored],
    chapter_spans: dict[int, tuple[float, float]],
    max_items: int = MAX_ITEMS,
    min_items: int = MIN_ITEMS,
) -> list[SuggestionItem]:
    ordered = sorted(scored, key=lambda cv: (-_value(cv[1]), cv[0].start_s))
    kept: list[SuggestionItem] = []
    used: set[str] = set()
    for c, v in ordered:
        if len(kept) >= max_items:
            break
        if _try_add(kept, c, v, v.effect, chapter_spans, reason(c, v)):
            used.add(c.id)
    # The owner asked for 3-6 moments: when the panel picks fewer, top up
    # from the best-ranked candidates it passed on, labelled as top-ups.
    for c, v in ordered:
        if len(kept) >= min_items:
            break
        if c.id not in used:
            effect = topup_effect(c)
            why = f"top-up ({effect.value}; panel picked fewer than {min_items}) · {reason(c, v)}"
            if _try_add(kept, c, v, effect, chapter_spans, why):
                used.add(c.id)
    return kept
