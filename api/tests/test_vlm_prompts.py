"""Pure prompt + schema tests for the VLM module.

No Ollama, no ffmpeg. Every test runs in <10ms.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ai_video_editor.vlm.prompts import (
    ClipVerdict,
    CompilationFix,
    CompilationReview,
    build_clip_system_prompt,
    build_clip_user_prompt,
    build_comp_system_prompt,
    build_comp_user_prompt,
    load_game_hints,
)

# ---------------------------------------------------------------------
# Verdict schema
# ---------------------------------------------------------------------


def test_clip_verdict_pass_shape() -> None:
    v = ClipVerdict.model_validate(
        {"verdict": "pass", "why": "clean cut", "fix": None, "fix_seconds": None}
    )
    assert v.verdict == "pass"
    assert v.fix is None


def test_clip_verdict_fixable_shape() -> None:
    v = ClipVerdict.model_validate(
        {
            "verdict": "fixable",
            "why": "kill too close to start",
            "fix": "extend_before",
            "fix_seconds": 2.0,
        }
    )
    assert v.verdict == "fixable"
    assert v.fix == "extend_before"
    assert v.fix_seconds == 2.0


def test_clip_verdict_false_positive_shape() -> None:
    v = ClipVerdict.model_validate(
        {"verdict": "false_positive", "why": "no fight, just chatter"}
    )
    assert v.verdict == "false_positive"
    assert v.fix is None


def test_clip_verdict_rejects_unknown_verdict() -> None:
    with pytest.raises(ValidationError):
        ClipVerdict.model_validate({"verdict": "kinda_ok", "why": "x"})


def test_clip_verdict_bounds_fix_seconds() -> None:
    # Upper bound — a hallucinated 999s extension should be caught.
    with pytest.raises(ValidationError):
        ClipVerdict.model_validate(
            {
                "verdict": "fixable",
                "why": "x",
                "fix": "extend_before",
                "fix_seconds": 100,
            }
        )


# ---------------------------------------------------------------------
# Compilation review schema
# ---------------------------------------------------------------------


def test_compilation_review_cohesive_empty_fixes() -> None:
    review = CompilationReview.model_validate(
        {"is_cohesive": True, "fixes": []}
    )
    assert review.is_cohesive is True
    assert review.fixes == []


def test_compilation_fix_apply_zoom_with_roi() -> None:
    fix = CompilationFix.model_validate(
        {
            "clip_ref": "03",
            "issue": "kill visually weak",
            "fix": "apply_zoom",
            "roi": "champion_portrait_lol",
        }
    )
    assert fix.fix == "apply_zoom"
    assert fix.roi == "champion_portrait_lol"


def test_compilation_fix_apply_focus_bounds() -> None:
    # focus_x / focus_y must be 0..1
    with pytest.raises(ValidationError):
        CompilationFix.model_validate(
            {
                "clip_ref": "05",
                "issue": "x",
                "fix": "apply_focus",
                "focus_x": 1.5,
                "focus_y": 0.5,
            }
        )


def test_compilation_fix_remove_clip_needs_no_extras() -> None:
    fix = CompilationFix.model_validate(
        {
            "clip_ref": "02",
            "issue": "duplicate of clip 01",
            "fix": "remove_clip",
        }
    )
    assert fix.fix == "remove_clip"
    assert fix.fix_seconds is None
    assert fix.roi is None


# ---------------------------------------------------------------------
# Game hints loader
# ---------------------------------------------------------------------


def test_load_game_hints_league_resolves_specific_file() -> None:
    text, resolved = load_game_hints("league")
    assert resolved == "league"
    # Verify content came from the real file — cheap sanity check
    assert "Killfeed" in text or "killfeed" in text


def test_load_game_hints_valorant_resolves_specific_file() -> None:
    text, resolved = load_game_hints("valorant")
    assert resolved == "valorant"
    assert "Ace" in text or "clutch" in text.lower()


def test_load_game_hints_unknown_game_falls_back_to_default() -> None:
    text, resolved = load_game_hints("nonexistent-game-42")
    assert resolved == "_default"
    assert text  # non-empty


def test_load_game_hints_none_game_falls_back_to_default() -> None:
    _, resolved = load_game_hints(None)
    assert resolved == "_default"


def test_load_game_hints_case_insensitive() -> None:
    _, resolved = load_game_hints("LEAGUE")
    assert resolved == "league"


# ---------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------


def test_build_clip_system_prompt_includes_hints_and_schema() -> None:
    prompt, resolved = build_clip_system_prompt("league")
    assert resolved == "league"
    # Structural sanity — the schema + the hints must both be present
    assert "false_positive" in prompt
    assert "fix_seconds" in prompt
    assert "Killfeed" in prompt or "killfeed" in prompt


def test_build_comp_system_prompt_includes_hints_and_fix_list() -> None:
    prompt, _ = build_comp_system_prompt("valorant")
    assert "remove_clip" in prompt
    assert "apply_zoom" in prompt
    assert "Ace" in prompt or "clutch" in prompt.lower()


def test_build_clip_user_prompt_terse_and_contextual() -> None:
    prompt = build_clip_user_prompt(
        event_type="kill",
        source="riot_api",
        anchor_seconds=754.2,
        clip_duration=12.5,
    )
    assert "kill" in prompt
    assert "riot_api" in prompt
    assert "754" in prompt
    assert "12.5" in prompt or "12.5s" in prompt


def test_build_comp_user_prompt_carries_shape() -> None:
    prompt = build_comp_user_prompt(clip_count=8, total_seconds=64.0)
    assert "64" in prompt
    assert "8" in prompt
