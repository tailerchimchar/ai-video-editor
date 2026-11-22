"""Typed decisions: ask questions about clips, get typed answers with a
probability from whichever backend is configured.

Backends (see `backends/`): Jev (TypeSafe System One, HTTP) when
TYPESAFE_API_KEY is set, Claude Haiku structured output only when
DECISIONS_BACKEND is explicitly "haiku", and free local rules
otherwise. `auto` never selects Haiku (cost-happy). Rules are also the
fallback whenever an LLM backend fails, so a decision never blocks the
pipeline.
"""

import logging

from ..config import settings
from .audit import log_answers, make_record
from .backends import BACKENDS
from .models import (
    Answer,
    BoolQuestion,
    ChoiceQuestion,
    DecisionError,
    ScoreQuestion,
    TypedQuestion,
)

__all__ = [
    "Answer",
    "BoolQuestion",
    "ChoiceQuestion",
    "DecisionError",
    "ScoreQuestion",
    "TypedQuestion",
    "ask",
    "ask_many",
    "select_backend",
]

_log = logging.getLogger(__name__)

_AUTO_ORDER = ("jev", "rules")


def select_backend() -> str:
    """The backend the next ask will use. A requested backend that is
    not available degrades to rules; `auto` is Jev if available, else
    rules, and never Haiku."""
    requested = settings.decisions_backend
    order = _AUTO_ORDER if requested == "auto" else (requested, "rules")
    return next(name for name in order if BACKENDS[name].available())


def ask_many(items: list[tuple[TypedQuestion, dict]]) -> list[Answer]:
    """Answer many `(question, state)` pairs, in order. Batching backends
    answer them in few calls; use this instead of looping `ask`."""
    if not items:
        return []
    name = select_backend()
    try:
        answers = BACKENDS[name].many(items)
    except DecisionError as exc:
        _log.warning("decisions: %s backend failed (%s); using rules", name, exc)
        name = "rules"
        answers = BACKENDS["rules"].many(items)
    _audit(name, items, answers)
    return answers


def _audit(name: str, items: list[tuple[TypedQuestion, dict]], answers: list[Answer]) -> None:
    """Log every answer; a logging failure must never change the result."""
    try:
        records = [
            make_record(name, question.prompt, state, answer)
            for (question, state), answer in zip(items, answers, strict=False)
        ]
        log_answers(records)
    except Exception as exc:
        _log.warning("decisions: audit failed (%s)", exc)


def ask(question: TypedQuestion, state: dict) -> Answer:
    """Answer one question about `state` (e.g. `{"clip": {...}}`)."""
    return ask_many([(question, state)])[0]
