"""Montage layout `9:16_hud` wraps the per-edit HUD builder, per indexed clip."""

from __future__ import annotations

import pytest

from ai_video_editor.shorts import _indexed_layout_chain


def test_hud_layout_has_scoreline_and_minimap_overlays() -> None:
    chain = _indexed_layout_chain("9:16_hud", 0, game="League of Legends")
    assert "iw*0.22:ih*0.10:iw*0.78:ih*0.005" in chain  # scoreline
    assert "ih*0.22:ih*0.22:iw-ih*0.23:ih*0.77" in chain  # minimap
    assert chain.endswith("[v0]")


def test_hud_layout_indexes_entry_and_internal_labels() -> None:
    c0 = _indexed_layout_chain("9:16_hud", 0, game="League of Legends")
    c2 = _indexed_layout_chain("9:16_hud", 2, game="League of Legends")
    assert "[2:v]" in c2 and "[0:v]" not in c2
    assert "[main0]" in c0 and "[main2]" in c2
    assert c2.endswith("[v2]")


def test_hud_layout_picks_valorant_variant() -> None:
    chain = _indexed_layout_chain("9:16_hud", 1, game="Valorant")
    assert "iw*0.22:ih*0.12:iw*0.78:ih*0.08" in chain


def test_unknown_layout_still_raises() -> None:
    with pytest.raises(ValueError, match="unknown shorts layout"):
        _indexed_layout_chain("nope", 0)
