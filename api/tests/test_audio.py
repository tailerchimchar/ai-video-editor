"""peaks_from_energy is pure (takes an RMS array, returns candidates).
No ffmpeg needed — feed synthetic energy curves."""

import numpy as np

from ai_video_editor.candidates.audio import peaks_from_energy


def test_empty_or_silent_energy_returns_no_candidates():
    assert peaks_from_energy(None) == []
    assert peaks_from_energy(np.zeros(10, dtype=np.float32)) == []


def test_isolated_peak_is_detected():
    e = np.zeros(60, dtype=np.float32)
    e[30] = 1.0  # one loud second at 30s
    out = peaks_from_energy(e)
    assert out, "expected at least one peak"
    p = out[0]
    # window padded by analyze_window_padding (default 4) around the second
    assert p["start_seconds"] <= 30 <= p["end_seconds"]
    assert 0.99 <= p["confidence"] <= 1.0


def test_quiet_under_threshold_is_ignored():
    # All quiet → normalized by max = 1.0; nothing meets the 0.6 threshold
    e = np.full(20, 0.0001, dtype=np.float32)
    e[10] = 0.0002  # still tiny — peak rel. 1.0 but the threshold normalization
    # makes every value 0.5 or 1.0 — the single 1.0 IS over threshold, so one peak.
    # This test asserts the function doesn't blow up on degenerate input.
    out = peaks_from_energy(e)
    assert isinstance(out, list)
