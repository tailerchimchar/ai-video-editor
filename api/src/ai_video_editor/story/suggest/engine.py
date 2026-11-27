"""Suggestion engines (open/closed: another engine implements
`SuggestionEngine`; nothing else changes).

`JevPanelEngine` asks the panel's three voices about every candidate in ONE
`decisions.ask_many` call (Jev batches one request per candidate state; rules
when Jev is not configured or fails; every answer audited, capped by
TYPESAFE_MAX_CALLS). No other model is ever called.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from ... import decisions
from ...decisions import Answer, TypedQuestion
from ..planner import StoryPlan
from .candidates import candidate_builder
from .models import SuggestionItem
from .questions import QUESTIONS_PER_CANDIDATE, questions_for
from .rank import MAX_ITEMS, rank
from .verdict import read_verdict

AskMany = Callable[[list[tuple[TypedQuestion, dict]]], list[Answer]]


class SuggestionEngine(Protocol):
    def suggest(self, plan: StoryPlan, timeline: Any, game: str) -> list[SuggestionItem]: ...


def _ask_many(items: list[tuple[TypedQuestion, dict]]) -> list[Answer]:
    # Looked up at call time so tests (and callers) can swap decisions.ask_many.
    return decisions.ask_many(items)


class JevPanelEngine:
    def __init__(self, ask: AskMany | None = None, max_items: int = MAX_ITEMS) -> None:
        self._ask = ask or _ask_many
        self._max_items = max_items

    def suggest(self, plan: StoryPlan, timeline: Any, game: str) -> list[SuggestionItem]:
        builder = candidate_builder(game)
        candidates = builder.build(plan, timeline)
        if not candidates:
            return []
        items = [q for c in candidates for q in questions_for(c)]
        answers = self._ask(items)
        n = QUESTIONS_PER_CANDIDATE
        scored = [
            (c, read_verdict(c, answers[i * n : (i + 1) * n])) for i, c in enumerate(candidates)
        ]
        return rank(scored, builder.chapter_spans(plan, timeline), self._max_items)
