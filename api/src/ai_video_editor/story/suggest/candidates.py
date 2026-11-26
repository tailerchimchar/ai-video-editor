"""Candidate builders, one per game (open/closed: a new game registers a
builder, nothing else changes).

A builder turns a rendered story plan plus that game's event data into the
`Candidate` moments the panel is asked about. Builders are pure code, no model.
"""

from __future__ import annotations

import importlib
from typing import Any, Protocol

from ..planner import StoryPlan
from .models import Candidate


class UnknownGameError(LookupError):
    """No candidate builder is registered for this game."""


class CandidateBuilder(Protocol):
    def build(self, plan: StoryPlan, timeline: Any) -> list[Candidate]: ...


_BUILDERS: dict[str, CandidateBuilder] = {}
# Modules that register the built-in builders on import (loaded on first lookup,
# so this module never imports a game-specific one at import time).
_BUILT_IN = {"league": ".league_candidates"}


def register_candidate_builder(game: str, builder: CandidateBuilder) -> None:
    _BUILDERS[game] = builder


def candidate_builder(game: str) -> CandidateBuilder:
    if game not in _BUILDERS and game in _BUILT_IN:
        importlib.import_module(_BUILT_IN[game], __package__)
    try:
        return _BUILDERS[game]
    except KeyError:
        raise UnknownGameError(f"no suggestion candidate builder for game {game!r}") from None
