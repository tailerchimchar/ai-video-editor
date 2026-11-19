"""Typed decisions: ask a question about a clip, get a typed answer with
a probability from whichever backend is configured.

Backends (see `backends/`): Jev when TYPESAFE_API_KEY is set, Claude
Haiku structured output when ANTHROPIC_API_KEY is set, and free local
rules otherwise. Rules are also the fallback whenever an LLM backend
fails, so a decision never blocks the pipeline.
"""

import logging
from collections.abc import Callable

from ..config import settings
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
    "select_backend",
]

_log = logging.getLogger(__name__)

_HAS_CREDENTIALS: dict[str, Callable[[], bool]] = {
    "jev": lambda: bool(settings.typesafe_api_key),
    "haiku": lambda: bool(settings.anthropic_api_key),
    "rules": lambda: True,
}
_AUTO_ORDER = ("jev", "haiku", "rules")


def select_backend() -> str:
    """The backend `ask` will use. A requested backend without its
    credentials degrades to rules; `auto` picks the first available."""
    requested = settings.decisions_backend
    order = _AUTO_ORDER if requested == "auto" else (requested, "rules")
    return next(name for name in order if _HAS_CREDENTIALS[name]())


def ask(question: TypedQuestion, state: dict) -> Answer:
    """Answer `question` about `state` (e.g. `{"clip": {...}}`)."""
    name = select_backend()
    try:
        return BACKENDS[name](question, state)
    except DecisionError as exc:
        _log.warning("decisions: %s backend failed (%s); using rules", name, exc)
        return BACKENDS["rules"](question, state)
