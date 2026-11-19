"""Jev backend (barebones seam): thin adapter over the typesafe-sdk shape.

Jev answers `choice` / `score` / `bool` questions, each returning a value
with a probability. The transport is injected so the adapter is tested
with a fake; no real transport exists yet because there is no
TYPESAFE_API_KEY to build one against. Wire `_default_transport` when
the key and the SDK are available.
"""

from typing import Protocol

from ..models import (
    Answer,
    BoolQuestion,
    ChoiceQuestion,
    DecisionError,
    ScoreQuestion,
    TypedQuestion,
)


class JevTransport(Protocol):
    """Each call returns `(value, probability)`."""

    def choice(self, prompt: str, options: list[str], state: dict) -> tuple[str, float]: ...

    def score(self, prompt: str, lo: int, hi: int, state: dict) -> tuple[int, float]: ...

    def bool(self, prompt: str, state: dict) -> tuple[bool, float]: ...


def default_transport() -> JevTransport | None:
    """The real typesafe-sdk transport, or None until it is wired (needs
    TYPESAFE_API_KEY and the SDK). `auto` selection requires it."""
    return None


def answer(question: TypedQuestion, state: dict, transport: JevTransport | None = None) -> Answer:
    transport = transport or default_transport()
    if transport is None:
        raise DecisionError("Jev transport is not wired yet: no typesafe-sdk client configured")
    if isinstance(question, ChoiceQuestion):
        options = [member.value for member in question.options]
        raw, probability = transport.choice(question.prompt, options, state)
        value = question.options(raw)
    elif isinstance(question, BoolQuestion):
        value, probability = transport.bool(question.prompt, state)
    elif isinstance(question, ScoreQuestion):
        value, probability = transport.score(question.prompt, question.lo, question.hi, state)
    else:
        raise DecisionError(f"unsupported question: {type(question).__name__}")
    return Answer(value=value, probability=probability, backend="jev")
