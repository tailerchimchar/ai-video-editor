"""Killfeed OCR verifier tests. Mocks tesseract + ffmpeg so the suite
stays fast and hermetic."""

from __future__ import annotations

import json
from pathlib import Path

from ai_video_editor.killfeed import (
    KillfeedTrack,
    _count_player_rows,
    _line_matches_name,
    verify_anchor,
)

# ---------------------------------------------------------------------
# Fuzzy name matching
# ---------------------------------------------------------------------


def test_line_matches_name_exact() -> None:
    assert _line_matches_name("Noodlz Vandal Sanoj", "Noodlz", 70)


def test_line_matches_name_ocr_noise() -> None:
    """Single-char OCR noise (l → 2, z → 7) fuzzy-matches at score 83."""
    assert _line_matches_name("Noodl2 Vandal Sanoj", "Noodlz", 70)
    assert _line_matches_name("Noodl7 Vandal Sanoj", "Noodlz", 70)


def test_line_matches_name_heavy_noise_rejected_by_default() -> None:
    """Two-char substitution (N00dlz) scores 67 — below the default
    70 threshold. Bump the threshold lower to accept, OR fix the OCR
    ROI so tesseract stops emitting 0-for-o."""
    assert not _line_matches_name("N00dlz Vandal Sanoj", "Noodlz", 70)
    assert _line_matches_name("N00dlz Vandal Sanoj", "Noodlz", 65)


def test_line_matches_rejects_unrelated_name() -> None:
    assert not _line_matches_name("Sanoj Phantom Sova", "Noodlz", 70)


def test_line_matches_empty_inputs() -> None:
    assert not _line_matches_name("", "Noodlz", 70)
    assert not _line_matches_name("Sanoj Vandal", "", 70)


def test_count_player_rows_multi_line() -> None:
    """Multi-line OCR with 2 rows both showing Noodlz as killer."""
    text = "Noodlz Vandal Sanoj\nNoodlz Sheriff Sova\nSanoj Phantom Cypher"
    assert _count_player_rows(text, "Noodlz", 70) == 2


def test_count_player_rows_returns_zero_when_absent() -> None:
    text = "Sanoj Vandal Cypher\nSova Sheriff Reyna"
    assert _count_player_rows(text, "Noodlz", 70) == 0


# ---------------------------------------------------------------------
# verify_anchor logic
# ---------------------------------------------------------------------


def test_verify_anchor_passes_through_when_track_none() -> None:
    """No track (verifier disabled / OCR missing) → verified passthrough.
    We do NOT want a missing verifier to drop real moments."""
    result = verify_anchor(15.0, None)
    assert result.verified is True
    assert result.snapped_anchor == 15.0
    assert result.matched_kill_time is None


def test_verify_anchor_snaps_within_tolerance() -> None:
    """A killfeed kill 1.2s away from the VLM anchor → verified,
    snapped to the killfeed time."""
    track = KillfeedTrack(
        clip_path=Path("/tmp/x.mp4"),
        player_name="Noodlz",
        game="Valorant",
        kill_timestamps=(15.5,),
        frame_count=30,
    )
    result = verify_anchor(14.3, track, tolerance_seconds=2.5)
    assert result.verified is True
    assert result.snapped_anchor == 15.5
    assert result.matched_kill_time == 15.5


def test_verify_anchor_picks_nearest_kill() -> None:
    """Multiple killfeed kills → snap to the closest."""
    track = KillfeedTrack(
        clip_path=Path("/tmp/x.mp4"),
        player_name="Noodlz",
        game="Valorant",
        kill_timestamps=(5.0, 20.0, 35.0),
        frame_count=60,
    )
    result = verify_anchor(21.0, track, tolerance_seconds=2.5)
    assert result.matched_kill_time == 20.0


def test_verify_anchor_snaps_beyond_tolerance_when_track_has_kills() -> None:
    """Shift-not-drop policy (2026-07-08): if OCR saw ANY kills, we
    snap to the nearest one even if it's beyond the tolerance window.
    Only drop when the track is empty — that's real VLM hallucination.
    Rationale: VLM is routinely 3-5s off on timing; OCR is ground
    truth."""
    track = KillfeedTrack(
        clip_path=Path("/tmp/x.mp4"),
        player_name="Noodlz",
        game="Valorant",
        kill_timestamps=(5.0, 40.0),
        frame_count=60,
    )
    result = verify_anchor(20.0, track, tolerance_seconds=2.5)
    # Nearest is 5.0 (delta 15) or 40.0 (delta 20). 5.0 wins.
    assert result.verified is True
    assert result.snapped_anchor == 5.0
    assert result.matched_kill_time == 5.0


def test_verify_anchor_drops_when_track_has_no_kills() -> None:
    """Empty killfeed track → nothing to verify against → drop."""
    track = KillfeedTrack(
        clip_path=Path("/tmp/x.mp4"),
        player_name="Noodlz",
        game="Valorant",
        kill_timestamps=(),
        frame_count=30,
    )
    result = verify_anchor(15.0, track)
    assert result.verified is False


# ---------------------------------------------------------------------
# Cache round-trip
# ---------------------------------------------------------------------


def test_killfeed_track_cache_shape(tmp_path) -> None:
    """Cache format is JSON with the fields we can load back."""
    from ai_video_editor.killfeed import _load_cached, _save_cached

    cache = tmp_path / "x.mp4.killfeed.json"
    track = KillfeedTrack(
        clip_path=Path("/tmp/x.mp4"),
        player_name="Noodlz",
        game="Valorant",
        kill_timestamps=(3.5, 17.0, 28.5),
        frame_count=30,
    )
    _save_cached(cache, track, backend="tesseract")
    payload = json.loads(cache.read_text(encoding="utf-8"))
    assert payload["player_name"] == "Noodlz"
    assert payload["backend"] == "tesseract"
    assert payload["kill_timestamps"] == [3.5, 17.0, 28.5]

    loaded = _load_cached(
        cache,
        expected_version=1,
        expected_player="Noodlz",
        expected_backend="tesseract",
    )
    assert loaded is not None
    assert loaded.kill_timestamps == (3.5, 17.0, 28.5)


def test_cache_invalidates_on_backend_mismatch(tmp_path) -> None:
    from ai_video_editor.killfeed import _load_cached, _save_cached

    cache = tmp_path / "x.mp4.killfeed.json"
    _save_cached(
        cache,
        KillfeedTrack(
            clip_path=Path("/tmp/x.mp4"),
            player_name="Noodlz",
            game="Valorant",
            kill_timestamps=(5.0,),
            frame_count=10,
        ),
        backend="tesseract",
    )
    # Same file but expecting a different backend → cache miss
    result = _load_cached(
        cache,
        expected_version=1,
        expected_player="Noodlz",
        expected_backend="paddleocr",
    )
    assert result is None


def test_cache_invalidates_on_player_mismatch(tmp_path) -> None:
    from ai_video_editor.killfeed import _load_cached, _save_cached

    cache = tmp_path / "x.mp4.killfeed.json"
    _save_cached(
        cache,
        KillfeedTrack(
            clip_path=Path("/tmp/x.mp4"),
            player_name="Noodlz",
            game="Valorant",
            kill_timestamps=(5.0,),
            frame_count=10,
        ),
        backend="tesseract",
    )
    result = _load_cached(
        cache,
        expected_version=1,
        expected_player="Sanoj",  # different player
        expected_backend="tesseract",
    )
    assert result is None
