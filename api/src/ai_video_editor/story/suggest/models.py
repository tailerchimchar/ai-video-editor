"""Shapes for editor suggestions (pinned contract, shared with the web UI).

Times on `Candidate` and `SuggestionItem` are game-clock seconds (what the
owner sees on the HUD), like `story/requests.py`.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel

SuggestionKind = Literal["letterbox_focus", "slowmo", "zoom"]
SetStatus = Literal["pending", "ready", "applied", "failed"]


class EffectChoice(str, Enum):
    """What the editing-expert voice picks for one moment."""

    none = "none"
    letterbox_focus = "letterbox_focus"
    slowmo = "slowmo"
    zoom = "zoom"


class Candidate(BaseModel):
    """One moment worth asking the panel about. `facts` is the whole state
    the panel sees (keys: minute, kills, deaths, numbers, outnumbered_win,
    multikill, objective, is_last_fight, enemy_champions): champions only,
    never mic text or summoner names. `anchor_s` (game clock, not shown to
    the panel) is the owner kill a slow-mo centres on, when there is one."""

    id: str
    chapter_index: int
    start_s: float
    end_s: float
    facts: dict
    anchor_s: float | None = None


class SuggestionItem(BaseModel):
    id: str
    kind: SuggestionKind
    chapter_index: int
    start_s: float
    end_s: float
    params: dict[str, float | str] = {}
    reason: str
    play_score: int | None = None
    pop_p: float | None = None
    backend: Literal["jev", "rules"]
    accepted: bool | None = None


class SuggestionSet(BaseModel):
    """`WORKSPACE/story/<round>/suggestions.json`."""

    round: str
    created_at: str
    status: SetStatus
    error: str | None = None
    items: list[SuggestionItem] = []
