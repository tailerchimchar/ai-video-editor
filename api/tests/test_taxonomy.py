"""Taxonomy vocabulary, legacy alias map, and the classifiers' use of it."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from ai_video_editor import bucketing, shorts
from ai_video_editor.taxonomy import (
    LEGACY_ALIASES,
    LEGACY_SLUGS,
    FlagCategory,
    Format,
    Intensity,
    PayoffType,
    Phase,
    Topic,
)
from ai_video_editor.vlm import prompts, watch_montage

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "taxonomy"


def test_contract_members() -> None:
    assert {m.value for m in Format} == {"hype", "story", "voice_over", "funny", "arc_montage"}
    assert {m.value for m in Topic} == {
        "first_blood", "outplay", "multikill", "teamfight",
        "objective_steal", "kite_escape", "tower_dive", "teamfight_turn",
    }  # fmt: skip
    assert {m.value for m in Phase} == {"laning", "mid", "late"}


def test_every_legacy_slug_maps_to_exactly_one_member() -> None:
    slugs = {*bucketing.BUCKETS, *shorts.BUCKET_ORDER}
    assert slugs <= LEGACY_ALIASES.keys()
    for slug in slugs:
        assert isinstance(LEGACY_ALIASES[slug], Format | Topic | Phase)


def test_phase_slugs_map_to_phase() -> None:
    assert LEGACY_ALIASES["laning_phase"] is Phase.laning
    assert LEGACY_ALIASES["mid_game"] is Phase.mid
    assert LEGACY_ALIASES["late_game"] is Phase.late


def test_legacy_slugs_is_the_inverse_of_aliases() -> None:
    assert {LEGACY_SLUGS[m]: m for m in LEGACY_SLUGS} == LEGACY_ALIASES


def test_bucket_order_is_derived_in_the_historic_order() -> None:
    assert shorts.BUCKET_ORDER == (
        "first_blood", "outplay", "multikill", "teamfight",
        "objective_steal", "laning_phase", "mid_game", "late_game",
    )  # fmt: skip


def test_payoff_type_keeps_today_values_and_adds_new_ones() -> None:
    values = {m.value for m in PayoffType}
    assert {
        "kill",
        "multi_kill",
        "ace",
        "clutch",
        "plant",
        "defuse",
        "save",
        "outplay",
        "other",
    } <= values
    assert {
        "first_blood", "multi_kill_2", "multi_kill_3", "multi_kill_4", "multi_kill_5",
        "objective_steal", "kite_escape", "tower_dive", "teamfight_turn",
    } <= values  # fmt: skip


def test_intensity_bands_and_range() -> None:
    assert Intensity(0.1).band == "low"
    assert Intensity(0.5).band == "mid"
    assert Intensity(0.7).band == "high"
    assert Intensity(0.95).band == "peak"
    assert Intensity(0.0) == 0.0
    with pytest.raises(ValueError):
        Intensity(1.1)
    with pytest.raises(ValueError):
        Intensity(-0.1)


def test_hype_routing_set_uses_payoff_members() -> None:
    assert PayoffType.ace in bucketing._HYPE_EVENT_TYPES
    assert bucketing.route_clip({"event": "multi_kill_3"})[0] == Format.hype


def test_categorize_clips_keeps_legacy_slugs() -> None:
    from ai_video_editor.shorts import ShortClip

    clip = ShortClip(
        file="a.mp4", path=Path("a.mp4"), event_type="kill",
        start_seconds=0, end_seconds=10, hype_score=0.9,
        funny_score=0.1, story_score=0.1, reason="",
    )  # fmt: skip
    assert list(shorts.categorize_clips([clip])) == ["first_blood", "laning_phase"]


def test_prompts_use_taxonomy_enums() -> None:
    assert prompts.PayoffType is PayoffType
    assert watch_montage.FlagCategory is FlagCategory


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_valorant_system_prompts_are_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prompt text is cache-keyed; pin it with live corrections isolated out."""
    monkeypatch.setattr(prompts, "load_recent_corrections", lambda *a, **k: [])
    expected = json.loads((FIXTURE_DIR / "valorant_prompts.json").read_text(encoding="utf-8"))
    assert _sha(prompts.build_clip_system_prompt("valorant")[0]) == expected["clip"]
    assert _sha(prompts.build_comp_system_prompt("valorant")[0]) == expected["comp"]
    assert _sha(prompts.build_analyzer_system_prompt("valorant")[0]) == expected["analyzer"]


def test_vlm_schemas_match_snapshot() -> None:
    """Any change to what the VLM may emit must show up in review."""
    models = (
        prompts.PayoffMoment,
        prompts.ClipAnalysis,
        watch_montage.MomentFlag,
        watch_montage.MontageCritique,
    )
    expected = json.loads((FIXTURE_DIR / "vlm_schemas.json").read_text(encoding="utf-8"))
    assert {m.__name__: m.model_json_schema() for m in models} == expected


def test_five_kill_routes_to_hype() -> None:
    assert bucketing.route_clip({"event": "multi_kill_5"})[0] == Format.hype
