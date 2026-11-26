"""Deterministic defaults from a candidate's facts, used whenever the panel
has no opinion (rules backend, or a Jev answer with probability 0), so the
"Get editor suggestions" button always produces something."""

from __future__ import annotations

from .models import EffectChoice


def default_effect(facts: dict) -> EffectChoice:
    if facts.get("is_last_fight"):
        return EffectChoice.letterbox_focus
    if facts.get("outnumbered_win"):
        return EffectChoice.slowmo
    if (facts.get("multikill") or 0) >= 2:
        return EffectChoice.zoom
    return EffectChoice.none


def default_score(facts: dict) -> int:
    multikill = facts.get("multikill") or 0
    if facts.get("is_last_fight"):
        return 5
    if facts.get("outnumbered_win") or multikill >= 3:
        return 4
    if multikill == 2:
        return 3
    return 2
