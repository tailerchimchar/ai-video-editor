"""Highlights: clip windowing strategies + folder-naming honesty. The
"don't let low-confidence Riot data name the folder" rule is enforced
here — that prevented the collision/mislabel bug we caught."""

from ai_video_editor.highlights import (
    _STRATEGIES,
    _champion,
    _clamp,
    _mmss,
    _parse_name,
    _safe,
    _window_anchor,
    _window_fallback,
    _window_full,
    cut_window,
    relative_folder,
)


def test_safe_strips_windows_illegal_chars():
    assert _safe("a/b\\c:d") == "a-b-c-d"
    assert _safe("") == "unknown"


def test_mmss_format():
    assert _mmss(0) == "0m00s"
    assert _mmss(125.7) == "2m05s"


def test_parse_name_standard_outplayed_filename():
    game, date, time_disc = _parse_name({"filename": "League of Legends_05-15-2026_21-19-8-0.mp4"})
    assert game == "League of Legends"
    assert date == "05-15-2026"
    assert time_disc == "21h19"


def test_parse_name_falls_back_when_unrecognized():
    g, d, _ = _parse_name({"filename": "random.mp4", "game": "Valorant_x"})
    assert g == "Valorant"  # split on first underscore
    assert d == "unknown-date"


def test_clamp_keeps_within_bounds_and_is_non_empty():
    assert _clamp(-2.0, 5.0, 10.0) == (0.0, 5.0)
    assert _clamp(8.0, 20.0, 10.0) == (8.0, 10.0)
    assert _clamp(5.0, 5.0, 10.0) == (5.0, 6.0)  # degenerate → 1s clip


def test_window_anchor_uses_explicit_anchor_then_midpoint():
    cand = {"start_seconds": 100, "end_seconds": 120, "metadata": {"anchor_seconds": 110}}
    s, e = _window_anchor(cand, {}, 200)
    # default pre/post = 7.5
    assert s == 102.5 and e == 117.5
    # No anchor → midpoint = (100+120)/2 = 110 → same window
    cand2 = {"start_seconds": 100, "end_seconds": 120, "metadata": {}}
    assert _window_anchor(cand2, {}, 200) == (s, e)


def test_window_full_returns_whole_file():
    assert _window_full({}, {}, 26.09) == (0.0, 26.09)


def test_window_fallback_prefers_llm_suggestion_then_candidate():
    cand = {"start_seconds": 50.0, "end_seconds": 70.0}
    rank = {"suggested_start_seconds": 55.0, "suggested_end_seconds": 65.0}
    assert _window_fallback(cand, rank, 200.0) == (55.0, 65.0)
    # without suggestion, fall back to candidate bounds
    assert _window_fallback(cand, {}, 200.0) == (50.0, 70.0)


def test_window_fallback_extends_by_event_type_padding():
    """When the candidate carries an event_type with an override, the
    ranker-suggested window is extended on each side. funny_audio default
    is (3.0, 6.0) → +3s pre, +6s post on top of ranker output."""
    cand = {"start_seconds": 50.0, "end_seconds": 70.0, "event_type": "funny_audio"}
    rank = {"suggested_start_seconds": 55.0, "suggested_end_seconds": 65.0}
    # 55 - 3 = 52, 65 + 6 = 71
    assert _window_fallback(cand, rank, 200.0) == (52.0, 71.0)


def test_window_fallback_no_padding_when_event_unknown():
    """Un-known event types (and missing event_type) get NO padding —
    preserves backward-compat for older un-tagged data + tests."""
    cand = {"start_seconds": 50.0, "end_seconds": 70.0, "event_type": "unrecognized_event"}
    rank = {"suggested_start_seconds": 55.0, "suggested_end_seconds": 65.0}
    assert _window_fallback(cand, rank, 200.0) == (55.0, 65.0)


def test_window_anchor_uses_per_event_pre_post_for_kills():
    """A kill candidate gets shorter pre + longer post (milk the celebration).
    Default override: kill = (3.0, 8.0)."""
    cand = {"start_seconds": 100, "end_seconds": 120,
            "event_type": "kill", "metadata": {"anchor_seconds": 110}}
    s, e = _window_anchor(cand, {}, 200)
    # 110 - 3 = 107, 110 + 8 = 118
    assert s == 107.0 and e == 118.0


def test_window_anchor_falls_back_to_globals_when_no_event_override():
    """riot_api candidates without event_type still get the global 7.5/7.5."""
    cand = {"start_seconds": 100, "end_seconds": 120, "metadata": {"anchor_seconds": 110}}
    s, e = _window_anchor(cand, {}, 200)
    assert s == 102.5 and e == 117.5


def test_window_anchor_extends_to_ranker_window_when_wider():
    """Rampage fix: when the ranker clustered multiple kills into one
    candidate, its suggested window is wider than the anchor rule alone.
    The anchor window should extend outward to the ranker's window."""
    cand = {"start_seconds": 100, "end_seconds": 120,
            "event_type": "kill", "metadata": {"anchor_seconds": 110}}
    # Ranker suggested a rampage 90 → 140s (spans several kills).
    rank = {"suggested_start_seconds": 90.0, "suggested_end_seconds": 140.0}
    s, e = _window_anchor(cand, rank, 200)
    # Anchor rule alone: 107 → 118. Ranker's window is wider both ends.
    # Union: min(107, 90) = 90 ; max(118, 140) = 140.
    assert s == 90.0
    assert e == 140.0


def test_window_anchor_ignores_ranker_window_when_narrower():
    """A narrow ranker window doesn't shrink the anchor-rule minimum."""
    cand = {"start_seconds": 100, "end_seconds": 120,
            "event_type": "kill", "metadata": {"anchor_seconds": 110}}
    # Ranker's window is inside the anchor window; anchor's floor holds.
    rank = {"suggested_start_seconds": 109.0, "suggested_end_seconds": 112.0}
    s, e = _window_anchor(cand, rank, 200)
    assert s == 107.0
    assert e == 118.0


def test_window_anchor_caps_at_max_seconds():
    """A pathological ranker window past `highlight_max_seconds` is
    clamped down. Prevents multi-minute clips from a mis-cluster."""
    from ai_video_editor.config import settings

    cand = {"start_seconds": 0, "end_seconds": 10,
            "event_type": "kill", "metadata": {"anchor_seconds": 5}}
    # Ranker suggests a 3-minute window — clamped to `highlight_max_seconds`.
    rank = {"suggested_start_seconds": 0.0, "suggested_end_seconds": 200.0}
    s, e = _window_anchor(cand, rank, 1000)
    assert e - s <= settings.highlight_max_seconds


def test_cut_window_dispatches_by_source_and_anchor():
    # Any candidate with anchor_seconds → anchor strategy (open/closed
    # for *future* sources)
    weird = {"source": "audio_peak", "metadata": {"anchor_seconds": 50}}
    s, e = cut_window(weird, {}, 100)
    assert s == 42.5 and e == 57.5
    # outplayed_clip → whole file
    op = {"source": "outplayed_clip", "metadata": {}}
    assert cut_window(op, {}, 30.0) == (0.0, 30.0)
    # The registry pattern stays open/closed
    assert "riot_api" in _STRATEGIES and "outplayed_clip" in _STRATEGIES


def test_champion_returns_first_riot_candidate_champion():
    cands = [
        {"source": "audio_peak", "metadata": {}},
        {"source": "riot_api", "metadata": {"champion": "Jhin"}},
        {"source": "riot_api", "metadata": {"champion": "Lucian"}},
    ]
    assert _champion(cands) == "Jhin"


def test_relative_folder_only_uses_champion_when_trusted():
    asset = {"filename": "League of Legends_05-15-2026_21-19-8-0.mp4"}
    # high-confidence → champion in folder
    trusted = [
        {
            "source": "riot_api",
            "metadata": {
                "champion": "Jhin",
                "correlation_confidence": "high",
            },
        }
    ]
    assert "Jhin" in str(relative_folder(asset, trusted))
    # low confidence → MUST fall back to time disc (no false naming /
    # collision with the real game's folder)
    untrusted = [
        {
            "source": "riot_api",
            "metadata": {
                "champion": "Lucian",
                "correlation_confidence": "low",
            },
        }
    ]
    folder = str(relative_folder(asset, untrusted))
    assert "Lucian" not in folder
    assert "21h19" in folder
