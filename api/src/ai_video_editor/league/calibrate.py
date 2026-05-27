"""Per-recording Riot↔recording offset detection.

The gap between when Outplayed starts a file and Riot's game-clock zero
(loading screen, champ select, dodges) is *constant within a recording
but different between recordings* — so it must be measured per file, not
configured globally.

Signal we already have: kills are loud (teamfights, the "Double Kill"
announcer). Slide the Riot kill pattern over the recording's own
per-second loudness; the shift where all kills land on loud spikes is
this recording's offset. Using the whole kill *comb* (their specific
spacings) makes the lock distinctive — only the true offset aligns them
all. Pure & testable: no I/O here.
"""

import numpy as np

# Tolerate a few seconds of slop between a kill's logged time and its
# audio spike (animation, multi-kill spread) by taking the local max.
_TOLERANCE_S = 3


def align_offset(
    kill_seconds: list[float], energy: np.ndarray | None, max_shift: int = 180
) -> tuple[float, float]:
    """Return (offset_seconds, quality).

    `offset_seconds`: add to a kill's game-clock time to get its recording
    offset. `quality`: how far the best alignment stands out from all
    other shifts, as a z-score (≳3 ≈ confident, <2 ≈ weak — verify/OCR).
    Returns (0.0, 0.0) when there's nothing to lock onto.
    """
    if energy is None or energy.size == 0 or not kill_seconds:
        return 0.0, 0.0

    e = energy.astype(np.float64)
    std = e.std()
    if std <= 0:
        return 0.0, 0.0
    e = (e - e.mean()) / std  # z-scored loudness

    n = e.size
    kills = np.asarray(kill_seconds, dtype=np.int64)
    shifts = np.arange(-max_shift, max_shift + 1)
    scores = np.empty(shifts.size, dtype=np.float64)

    for i, tau in enumerate(shifts):
        idx = kills + tau
        total = 0.0
        for c in idx:
            lo, hi = max(0, c - _TOLERANCE_S), min(n, c + _TOLERANCE_S + 1)
            if lo < hi:
                total += e[lo:hi].max()
        scores[i] = total

    best = int(scores.argmax())
    s_mean, s_std = scores.mean(), scores.std()
    quality = float((scores[best] - s_mean) / s_std) if s_std > 0 else 0.0
    return float(shifts[best]), round(quality, 2)
