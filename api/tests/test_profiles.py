"""Sprint #2 — game profile registry."""

from __future__ import annotations

import pytest

from ai_video_editor.profiles import (
    Profile,
    ProfileMeta,
    Region,
    Sound,
    _registry,
    load_profile,
    region_box,
    sound_path,
)


@pytest.fixture(autouse=True)
def _clear_profile_cache():
    """Profiles are lru_cached for the process; reset between tests so
    a per-test monkeypatch on workspace_dir is honored by sound_path."""
    _registry.cache_clear()
    yield
    _registry.cache_clear()


# ----- Region validation -----


def test_region_coords_must_be_in_unit_box():
    Region(x=0.0, y=0.0, w=0.5, h=0.5)  # ok
    Region(x=0.5, y=0.5, w=0.5, h=0.5)  # right-edge ok
    with pytest.raises(ValueError):
        Region(x=-0.1, y=0, w=0.5, h=0.5)
    with pytest.raises(ValueError):
        Region(x=0, y=1.1, w=0.5, h=0.5)
    with pytest.raises(ValueError):
        Region(x=0, y=0, w=0.0, h=0.5)  # w must be > 0
    with pytest.raises(ValueError):
        Region(x=0, y=0, w=1.5, h=0.5)


def test_region_rejects_box_extending_past_frame():
    with pytest.raises(ValueError, match="right edge"):
        Region(x=0.7, y=0.0, w=0.5, h=0.5)
    with pytest.raises(ValueError, match="bottom edge"):
        Region(x=0.0, y=0.7, w=0.5, h=0.5)


# ----- Registry & lookup -----


def test_registry_indexes_canonical_and_aliases():
    reg = _registry()
    # League is keyed by canonical name + every alias, lowercased.
    assert "league" in reg
    assert "league of legends" in reg
    assert "lol" in reg
    assert "valorant" in reg
    assert "val" in reg
    assert "default" in reg


def test_load_profile_resolves_by_alias_case_insensitive():
    assert load_profile("League of Legends").meta.game == "league"
    assert load_profile("LEAGUE OF LEGENDS").meta.game == "league"
    assert load_profile("Valorant").meta.game == "valorant"
    assert load_profile("VAL").meta.game == "valorant"


def test_load_profile_unknown_game_falls_back_to_default():
    assert load_profile("Bowling").meta.game == "default"
    assert load_profile(None).meta.game == "default"
    assert load_profile("").meta.game == "default"


def test_default_profile_has_empty_vocab():
    p = load_profile(None)
    assert p.regions == {}
    assert p.sounds == {}


def test_load_profile_strips_outplayed_date_suffix():
    """`asset.game` arrives as e.g. 'League of Legends_05-22-2026_0-53-57-757'
    (Outplayed session-folder names). load_profile must strip the date
    suffix and match the leading game name against the alias table."""
    p = load_profile("League of Legends_05-22-2026_0-53-57-757")
    assert p.meta.game == "league"
    p = load_profile("Valorant_05-21-2026_22-36-25-124")
    assert p.meta.game == "valorant"


def test_load_profile_direct_match_wins_over_suffix_strip():
    """If a profile alias contains an underscore intentionally, a direct
    match beats the suffix-strip fallback."""
    # 'League_of_Legends' is an alias in league.toml — direct match.
    assert load_profile("League_of_Legends").meta.game == "league"


def test_league_profile_has_expected_v1_vocab():
    p = load_profile("league")
    expected_regions = {
        "minimap",
        "scoreboard",
        "tab_overlay",
        "killfeed",
        "champion_portrait",
        "item_bar",
        "hp_mana",
    }
    assert set(p.regions) == expected_regions
    assert "first_blood" in p.sounds
    assert "buy_item" in p.sounds


def test_valorant_profile_has_expected_v1_vocab():
    p = load_profile("Valorant")
    expected_regions = {
        "minimap",
        "scoreboard",
        "tab_overlay",
        "killfeed",
        "crosshair",
        "agent_card",
        "ult_orbs",
        "money_display",
    }
    assert set(p.regions) == expected_regions
    assert "headshot_ping" in p.sounds
    assert "cash_pickup" in p.sounds


# ----- region_box helper -----


def test_region_box_returns_named_region():
    box = region_box("league", "minimap")
    assert box is not None
    assert 0.0 <= box.x <= 1.0 and 0.0 <= box.y <= 1.0


def test_region_box_unknown_region_returns_none():
    assert region_box("league", "no_such_region") is None


def test_region_box_unknown_game_returns_none():
    # default profile has no regions, so any name -> None.
    assert region_box("Bowling", "minimap") is None


# ----- sound_path helper -----


def test_sound_path_unknown_sound_returns_none(sandbox):
    assert sound_path("league", "no_such_sound") is None


def test_sound_path_returns_none_when_file_missing(sandbox):
    # 'first_blood' is declared in the profile but the workspace file
    # hasn't been extracted yet -> None (warn-and-skip at callsite).
    assert sound_path("league", "first_blood") is None


def test_sound_path_resolves_to_workspace_file(sandbox):
    from ai_video_editor.config import settings

    sfx_dir = settings.workspace_dir / "media_library" / "league" / "sfx"
    sfx_dir.mkdir(parents=True, exist_ok=True)
    (sfx_dir / "first_blood.wav").write_bytes(b"RIFF\x00\x00\x00\x00WAVE")
    path = sound_path("league", "first_blood")
    assert path is not None
    assert path.name == "first_blood.wav"
    assert path.exists()


def test_sound_path_uses_canonical_game_dir_for_aliases(sandbox):
    """Aliases ('League of Legends', 'lol', ...) all resolve to the
    same 'league' directory — so extracted files are shared regardless
    of which alias was used to extract them."""
    from ai_video_editor.config import settings

    sfx_dir = settings.workspace_dir / "media_library" / "league" / "sfx"
    sfx_dir.mkdir(parents=True, exist_ok=True)
    (sfx_dir / "ace.wav").write_bytes(b"x")
    for alias in ("league", "LEAGUE", "League of Legends", "lol"):
        assert sound_path(alias, "ace") is not None


# ----- Profile model can be constructed in code (used by extraction tests) -----


def test_profile_can_be_constructed_in_memory():
    p = Profile(
        meta=ProfileMeta(game="test", aliases=["t"], default_aspect="9:16"),
        regions={"box": Region(x=0.1, y=0.1, w=0.2, h=0.2)},
        sounds={"ping": Sound(file="ping.wav")},
    )
    assert p.meta.default_aspect == "9:16"
    assert p.regions["box"].w == 0.2
