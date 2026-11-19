"""Question and answer types shared by every decision backend."""

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel


class DecisionError(RuntimeError):
    """A backend could not produce an answer; `ask` falls back to rules."""


class ChoiceQuestion(BaseModel):
    """Pick one member of `options` (e.g. `taxonomy.Format`)."""

    prompt: str
    options: type[Enum]


class BoolQuestion(BaseModel):
    """Yes/no. The answer's probability is confidence in its own value."""

    prompt: str


class ScoreQuestion(BaseModel):
    """An integer in `lo..hi`."""

    prompt: str
    lo: int
    hi: int


TypedQuestion = ChoiceQuestion | BoolQuestion | ScoreQuestion


class Answer(BaseModel):
    """`value` is an enum member, bool or int by question kind, or None
    when the backend has no opinion (probability 0)."""

    value: Any
    probability: float
    backend: Literal["jev", "haiku", "rules"]
