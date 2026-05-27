"""The kill↔audio cross-correlator. We know audio offset detection can
false-lock (it bit us in real footage), so the contract these tests
enforce is honest: it FINDS planted offsets in synthetic data, and
reports LOW quality when there's no signal."""

import numpy as np

from ai_video_editor.league.calibrate import align_offset


def _energy_with_planted_offset(n: int, kills: list[int], offset: int) -> np.ndarray:
    e = np.full(n, 0.01, dtype=np.float32)
    rng = np.random.default_rng(0)
    e += rng.random(n).astype(np.float32) * 0.05  # quiet noise
    for k in kills:
        idx = k + offset
        if 0 <= idx < n:
            e[idx] = 1.0  # loud spike at the true offset
    return e


def test_align_offset_locks_planted_offset():
    kills = [120, 127, 300, 455, 800, 1190, 1500]
    truth = 47
    e = _energy_with_planted_offset(1800, kills, truth)
    found, quality = align_offset(kills, e)
    assert abs(found - truth) <= 3  # within tolerance window
    assert quality >= 3.0  # confident lock


def test_align_offset_returns_zero_for_empty_inputs():
    assert align_offset([], np.zeros(10)) == (0.0, 0.0)
    assert align_offset([10, 20], None) == (0.0, 0.0)


def test_align_offset_low_quality_on_pure_noise():
    # No planted signal → low z-score (we may still report *a* shift,
    # but quality should be unimpressive). Asserting weakness, not exact
    # offset, since the model can latch onto random alignments.
    rng = np.random.default_rng(0)
    e = rng.random(1800).astype(np.float32)
    _, quality = align_offset([100, 200, 300, 400, 500], e)
    assert quality < 3.0
