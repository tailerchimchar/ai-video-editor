"""Read one candidate's three answers (gaming, viewer, editor) into a
`Verdict`. An answer from the rules backend, or with probability 0, is no
opinion: the facts-based default (`defaults.py`) stands in for it."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ...decisions import Answer
from .defaults import default_effect, default_score
from .models import Candidate, EffectChoice


@dataclass(frozen=True)
class Verdict:
    score: int
    score_p: float | None  # None = default score (no opinion)
    pop_p: float | None  # probability the viewer says "pop"; None = no opinion
    effect: EffectChoice
    effect_p: float | None  # None = default effect (no opinion)
    backend: Literal["jev", "rules"]


def has_opinion(answer: Answer) -> bool:
    return answer.backend != "rules" and answer.probability > 0 and answer.value is not None


def read_verdict(candidate: Candidate, answers: list[Answer]) -> Verdict:
    gaming, viewer, editor = answers
    facts = candidate.facts
    score, score_p = (
        (int(gaming.value), gaming.probability)
        if has_opinion(gaming)
        else (default_score(facts), None)
    )
    pop_p = None
    if has_opinion(viewer):
        pop_p = viewer.probability if viewer.value else 1.0 - viewer.probability
    effect, effect_p = (
        (EffectChoice(editor.value), editor.probability)
        if has_opinion(editor)
        else (default_effect(facts), None)
    )
    any_jev = any(has_opinion(a) for a in answers)
    return Verdict(score, score_p, pop_p, effect, effect_p, "jev" if any_jev else "rules")
