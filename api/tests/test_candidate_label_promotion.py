"""Tests for `promote_audio_event_types` — pure candidate-label fixup.

Runs entirely in memory; no ffmpeg, no DB, no Riot.
"""

from __future__ import annotations

from ai_video_editor.candidates.service import promote_audio_event_types


def _audio_row(start: float, end: float, event: str = "audio_peak") -> dict:
    return {
        "id": f"a-{start}",
        "source": "audio_peak",
        "start_seconds": start,
        "end_seconds": end,
        "event_type": event,
        "confidence": 0.5,
        "metadata": {},
    }


def _riot_row(start: float, end: float, event: str = "kill", conf: float = 0.9) -> dict:
    return {
        "id": f"r-{start}",
        "source": "riot_api",
        "start_seconds": start,
        "end_seconds": end,
        "event_type": event,
        "confidence": conf,
        "metadata": {"anchor_seconds": start},
    }


def test_no_visible_events_leaves_audio_alone() -> None:
    rows = [_audio_row(10, 15), _audio_row(20, 25)]
    out = promote_audio_event_types(rows)
    assert out == rows
    assert all(r["event_type"] == "audio_peak" for r in out)


def test_visible_kill_overlap_promotes_audio_label() -> None:
    audio = _audio_row(10, 20)
    kill = _riot_row(12, 13, "kill")
    out = promote_audio_event_types([audio, kill])
    promoted = next(r for r in out if r["source"] == "audio_peak")
    assert promoted["event_type"] == "kill"
    assert promoted["metadata"]["promoted_from"] == "audio_peak"
    assert promoted["metadata"]["promoted_by_source"] == "riot_api"


def test_no_overlap_leaves_audio_alone() -> None:
    audio = _audio_row(10, 20)
    kill = _riot_row(80, 81, "kill")  # far away — beyond tolerance
    out = promote_audio_event_types([audio, kill])
    promoted = next(r for r in out if r["source"] == "audio_peak")
    assert promoted["event_type"] == "audio_peak"
    assert "promoted_from" not in (promoted.get("metadata") or {})


def test_within_tolerance_promotes_even_without_overlap() -> None:
    """The proximity fix: an audio_peak that anticipates or celebrates
    a kill by ~15s should still promote. Strict overlap would miss it."""
    audio = _audio_row(932, 940)  # short 8s peak
    kill = _riot_row(964, 965, "kill")  # 24s later — no overlap
    # Default tolerance is 20s; 24s gap must NOT promote.
    out = promote_audio_event_types([audio, kill])
    promoted = next(r for r in out if r["source"] == "audio_peak")
    assert promoted["event_type"] == "audio_peak"
    # Bump the tolerance and it does promote.
    out2 = promote_audio_event_types([audio, kill], tolerance_seconds=30.0)
    promoted2 = next(r for r in out2 if r["source"] == "audio_peak")
    assert promoted2["event_type"] == "kill"


def test_promote_within_default_tolerance() -> None:
    """15s gap between short audio peak and kill — under the 20s default
    tolerance, promotes."""
    audio = _audio_row(1000, 1005)
    kill = _riot_row(1015, 1016, "kill")  # 10s past audio's end
    out = promote_audio_event_types([audio, kill])
    promoted = next(r for r in out if r["source"] == "audio_peak")
    assert promoted["event_type"] == "kill"


def test_cv_kda_source_also_promotes() -> None:
    audio = _audio_row(10, 20)
    death = {
        "id": "cv-1",
        "source": "cv_kda",
        "start_seconds": 14,
        "end_seconds": 15,
        "event_type": "death",
        "confidence": 0.8,
        "metadata": {},
    }
    out = promote_audio_event_types([audio, death])
    promoted = next(r for r in out if r["source"] == "audio_peak")
    assert promoted["event_type"] == "death"
    assert promoted["metadata"]["promoted_by_source"] == "cv_kda"


def test_transcript_keyword_is_also_audio_derived() -> None:
    hype = {
        "id": "t-1",
        "source": "transcript_keyword",
        "start_seconds": 11,
        "end_seconds": 12,
        "event_type": "hype_callout",
        "confidence": 0.7,
        "metadata": {},
    }
    kill = _riot_row(11, 12, "kill")
    out = promote_audio_event_types([hype, kill])
    promoted = next(r for r in out if r["source"] == "transcript_keyword")
    assert promoted["event_type"] == "kill"


def test_visible_source_but_non_gameplay_event_does_not_promote() -> None:
    # cv_kda might emit non-gameplay events (e.g. HUD glitch);
    # we only promote against the _VISIBLE_EVENTS whitelist.
    audio = _audio_row(10, 20)
    junk = {
        "id": "cv-junk",
        "source": "cv_kda",
        "start_seconds": 12,
        "end_seconds": 13,
        "event_type": "hud_flicker",  # not in whitelist
        "confidence": 0.9,
        "metadata": {},
    }
    out = promote_audio_event_types([audio, junk])
    promoted = next(r for r in out if r["source"] == "audio_peak")
    assert promoted["event_type"] == "audio_peak"


def test_riot_row_itself_untouched() -> None:
    audio = _audio_row(10, 20)
    kill = _riot_row(12, 13, "kill")
    out = promote_audio_event_types([audio, kill])
    riot = next(r for r in out if r["source"] == "riot_api")
    assert riot["event_type"] == "kill"
    assert "promoted_from" not in (riot.get("metadata") or {})


def test_multiple_overlaps_picks_highest_confidence() -> None:
    audio = _audio_row(10, 30)
    low = _riot_row(12, 13, "assist", conf=0.4)
    high = _riot_row(20, 21, "kill", conf=0.95)
    out = promote_audio_event_types([audio, low, high])
    promoted = next(r for r in out if r["source"] == "audio_peak")
    # Higher-confidence match wins
    assert promoted["event_type"] == "kill"


def test_preserves_row_order_and_count() -> None:
    audio1 = _audio_row(10, 15)
    audio2 = _audio_row(30, 35)
    kill = _riot_row(12, 13, "kill")
    out = promote_audio_event_types([audio1, kill, audio2])
    assert len(out) == 3
    assert out[0]["id"] == audio1["id"]
    assert out[1]["id"] == kill["id"]
    assert out[2]["id"] == audio2["id"]
