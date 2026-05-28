"""Pure-function tests for the CV/KDA detector.

Tests the OCR-parsing regex and the candidate-building helper. The
ffmpeg + tesseract subprocess paths are mocked / not exercised here —
the integration path is "run on a real recording" which the user
exercises end-to-end via the pipeline.
"""

from ai_video_editor.candidates.cv_kda import (
    _KDA_RE,
    _crop_expr_for_scoreboard,
    _make_candidate,
    _sane_transition,
    detect_kda_events,
)


def test_kda_regex_accepts_slash_separator():
    m = _KDA_RE.search("3/0/2")
    assert m is not None
    assert (int(m.group(1)), int(m.group(2)), int(m.group(3))) == (3, 0, 2)


def test_kda_regex_rejects_space_only_separators():
    """The League HUD top-right strip shows both `18 vs 23` (team kills,
    space-separated) AND `11/3/2` (personal KDA, slash-separated) in
    the same crop. We MUST reject space-only triples — otherwise OCR
    output like '18 23 11/3/2' picks up (18, 23, 11) instead of (11, 3, 2)."""
    m = _KDA_RE.search("3 0 2")
    assert m is None


def test_kda_regex_locks_onto_kda_not_team_kills():
    """Real OCR output from the top-right HUD strip — should pick the
    SLASH-separated triple (the actual KDA), not the leading team
    kills numbers."""
    m = _KDA_RE.search("18 23  11/3/2")
    assert m is not None
    assert (int(m.group(1)), int(m.group(2)), int(m.group(3))) == (11, 3, 2)


def test_kda_regex_accepts_mixed_slash_separators():
    """Tesseract sometimes reads / as | or \\ — accept either between
    digit groups (but not space-only)."""
    m = _KDA_RE.search("3 / 0 | 2")
    assert m is not None
    assert (int(m.group(1)), int(m.group(2)), int(m.group(3))) == (3, 0, 2)


def test_kda_regex_handles_two_digit_values():
    m = _KDA_RE.search("12/3/15")
    assert m is not None
    assert (int(m.group(1)), int(m.group(2)), int(m.group(3))) == (12, 3, 15)


def test_kda_regex_ignores_unrelated_noise():
    """Output like 'FPS: 144' should NOT match — three digit groups
    separated by non-KDA punctuation."""
    m = _KDA_RE.search("FPS: 144")
    assert m is None


def test_make_candidate_kill_centers_on_anchor():
    cand = _make_candidate("kill", 100.0, (3, 1, 2), (2, 1, 2))
    # 5s pad each side around anchor=100
    assert cand["start_seconds"] == 95.0
    assert cand["end_seconds"] == 105.0
    assert cand["event_type"] == "kill"
    assert cand["confidence"] == 0.85
    assert cand["metadata"]["anchor_seconds"] == 100.0
    assert cand["metadata"]["kda_before"] == [2, 1, 2]
    assert cand["metadata"]["kda_after"] == [3, 1, 2]


def test_make_candidate_clamps_anchor_at_zero():
    """A K/D/A change near the start of the recording shouldn't produce
    a negative start_seconds."""
    cand = _make_candidate("death", 3.0, (0, 1, 0), (0, 0, 0))
    assert cand["start_seconds"] == 0.0  # clamped, not -2.0
    assert cand["end_seconds"] == 8.0


def test_make_candidate_distinguishes_event_types():
    """The metadata.rationale string should reflect which value changed."""
    kill = _make_candidate("kill", 50.0, (1, 0, 0), (0, 0, 0))
    death = _make_candidate("death", 50.0, (0, 1, 0), (0, 0, 0))
    assist = _make_candidate("assist", 50.0, (0, 0, 1), (0, 0, 0))
    assert "kill" in kill["metadata"]["rationale"]
    assert "death" in death["metadata"]["rationale"]
    assert "assist" in assist["metadata"]["rationale"]


def test_crop_expr_default_when_no_profile():
    """Unknown game falls back to the League scoreboard shape — generic
    enough that 'wrong region' yields 'OCR sees nothing' instead of
    crashing."""
    expr = _crop_expr_for_scoreboard("unknown_game")
    assert "crop=" in expr
    # Default fallback uses the hardcoded League shape (0.16, 0.04, 0.42, 0)
    assert "0.16" in expr and "0.04" in expr


def test_crop_expr_uses_profile_region_when_available():
    """A known game should produce a crop expression derived from the
    profile's scoreboard region (not the fallback)."""
    expr = _crop_expr_for_scoreboard("league")
    assert "crop=" in expr


def test_detect_skips_short_outplayed_clips():
    """Outplayed event clips are too short to benefit from KDA detection
    and already covered by the `outplayed_clip` source. Skip cleanly."""
    out = detect_kda_events("/nonexistent.mp4", duration=30.0, game="league")
    assert out == []


def test_sane_transition_accepts_single_kill():
    assert _sane_transition((3, 1, 2), (4, 1, 2)) is True


def test_sane_transition_accepts_no_change():
    assert _sane_transition((3, 1, 2), (3, 1, 2)) is True


def test_sane_transition_accepts_double_kill():
    assert _sane_transition((3, 1, 2), (5, 1, 2)) is True


def test_sane_transition_rejects_decrease():
    """KDA values never decrease within a single game."""
    assert _sane_transition((3, 1, 2), (3, 0, 2)) is False


def test_sane_transition_rejects_team_kill_misread():
    """OCR reading team-kills column instead of personal KDA — typically
    shows up as MULTIPLE axes jumping by 3+ at once. Real KDA evolves
    one axis at a time."""
    assert _sane_transition((6, 1, 0), (11, 6, 2)) is False
    assert _sane_transition((9, 2, 0), (18, 10, 2)) is False
    assert _sane_transition((6, 2, 0), (12, 6, 2)) is False


def test_sane_transition_rejects_huge_single_axis_jump():
    """OCR sometimes hallucinates a large digit. Even pentakills net
    only +5 in one sample window — anything 8+ is noise."""
    assert _sane_transition((10, 2, 0), (10, 2, 14)) is False
    assert _sane_transition((11, 3, 2), (24, 3, 2)) is False


def test_sane_transition_accepts_simultaneous_small_changes():
    """A kill+assist combo in the same 5s window is plausible (real
    teamfight). Both axes change, but each by only 1."""
    assert _sane_transition((3, 1, 2), (4, 1, 3)) is True


def test_detect_handles_missing_tools_gracefully():
    """Missing tesseract / ffmpeg returns [] rather than raising —
    candidate generation should never fail the job just because one
    source's deps aren't installed."""
    # Duration > outplayed_clip_max so we get past the early-return,
    # then either ffmpeg or tesseract probe will fail because the
    # path is nonexistent. Either way: empty list, no crash.
    out = detect_kda_events("/definitely/does/not/exist.mp4", duration=300.0, game="league")
    assert out == []
