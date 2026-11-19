"""Typed decisions: ask questions about clips, get typed answers with a
probability from whichever backend is configured.

Backends (see `backends/`): Jev when TYPESAFE_API_KEY is set and a Jev
transport exists, Claude Haiku structured output only when
DECISIONS_BACKEND is explicitly "haiku", and free local rules
otherwise. `auto` never selects Haiku (cost-happy). Rules are also the
fallback whenever an LLM backend fails, so a decision never blocks the
pipeline.
"""

import logging
from collections.abc import Callable

from ..config import settings
from .backends import BACKENDS, jev
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

_AVAILABLE: dict[str, Callable[[], bool]] = {
    "jev": lambda: bool(settings.typesafe_api_key) and jev.default_transport() is not None,
    "haiku": lambda: bool(settings.anthropic_api_key),
    "rules": lambda: True,
}
_AUTO_ORDER = ("jev", "rules")


def select_backend() -> str:
    """The backend the next ask will use. A requested backend that is
    not available degrades to rules; `auto` is Jev if available, else
    rules, and never Haiku."""
    requested = settings.decisions_backend
    order = _AUTO_ORDER if requested == "auto" else (requested, "rules")
    return next(name for name in order if _AVAILABLE[name]())


def ask_many(items: list[tuple[TypedQuestion, dict]]) -> list[Answer]:
    """Answer many `(question, state)` pairs, in order. Batching backends
    answer them in few calls; use this instead of looping `ask`."""
    if not items:
        return []
    name = select_backend()
    try:
        return BACKENDS[name].many(items)
    except DecisionError as exc:
        _log.warning("decisions: %s backend failed (%s); using rules", name, exc)
        return BACKENDS["rules"].many(items)


def ask(question: TypedQuestion, state: dict) -> Answer:
    """Answer one question about `state` (e.g. `{"clip": {...}}`)."""
    return ask_many([(question, state)])[0]
