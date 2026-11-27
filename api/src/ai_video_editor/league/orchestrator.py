"""LoL candidate orchestrator.

Concentrates LoL-specific candidate logic — for now, the Riot kill
timeline plus its per-recording offset calibration. Future LoL sources
(champion ID, killfeed CV) plug in here without touching the generic
`candidates.service` flow.

Inputs are deliberately small and pure: `asset`, the recording's
duration, and (optionally) the per-second loudness curve that the
generic flow already decoded — we reuse it for offset alignment so we
never decode audio twice.

Returns `(rows, riot_status)`. `rows` are HighlightCandidate-shaped
dicts ready to extend the generic flow's list. `riot_status` is the
same string field that `candidates.service` surfaces to the job
summary (`ok | no_match | rate_limited | api_error | disabled`).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from ..config import settings
from .calibrate import align_offset
from .candidates.riot import detect_riot_events
from .ocr import verify_offset
from .offset import OffsetResult, calibrate_offset
from .riot_timeline import (
    DRIFT_WARN_S,
    HUD_SOURCE,
    cache_dir_for,
    hud_offset,
    offset_drift_s,
    pre_pad_for_drift,
    record_offset,
)

_log = logging.getLogger(__name__)

# Fight bounds from riot.py are in riot.py's recording seconds
# (game clock + RIOT_SYNC_OFFSET_SECONDS); shifted here to the calibrated offset.
_FIGHT_KEYS = ("fight_start_s", "fight_end_s", "game_end_s")
_MIN_KILLS_PER_HALF = 2


def _drift_pre_pad(kills: list[float], offset: float, energy, duration: float) -> float | None:
    """R3 piecewise check: align each half of the recording separately; a
    disagreement > 3 s widens the pre-pad to 8 s. Returns the widened pad, or
    None when the halves agree or there is too little signal to judge.
    Never raises: a drift-check failure must not break candidates."""
    if energy is None or duration <= 0:
        return None
    half = duration / 2.0
    first = [k for k in kills if k + offset < half]
    if len(first) < _MIN_KILLS_PER_HALF or len(kills) - len(first) < _MIN_KILLS_PER_HALF:
        return None

    def align(rng: tuple[float, float]) -> float:
        lo, hi = rng
        return align_offset([k for k in kills if lo <= k + offset < hi], energy)[0]

    try:
        drift = offset_drift_s(align, duration_s=duration)
    except Exception as e:  # optional diagnostic
        _log.warning("Riot offset drift check skipped: %s", e)
        return None
    if drift <= DRIFT_WARN_S:
        return None
    _log.warning("Riot offset drift %.1fs between recording halves; widening pre-pad", drift)
    return pre_pad_for_drift(drift)


def gather_lol_candidates(
    asset: dict, duration: float, energy: list[float] | None
) -> tuple[list[dict], str]:
    """Run LoL-only candidate sources for one recording.

    `energy` is the per-second loudness curve from the generic flow's
    audio decode (shared so we don't decode twice). It can be None for
    short recordings where the generic flow didn't run audio_peak.
    """
    video_id = asset["id"]
    now = datetime.now(timezone.utc).isoformat()
    rows: list[dict] = []

    riot_rows, riot_status = detect_riot_events(asset, duration)
    if not riot_rows:
        return rows, riot_status

    # Per-recording offset between the file start and Riot's game clock.
    # Authoritative path is a human-measured override (auto methods lie);
    # otherwise align the kill comb against this recording's loudness,
    # with an OCR clock read as a tiebreaker if the lock is weak.
    kills = sorted((r["metadata"]["riot_event_ms"]) / 1000.0 for r in riot_rows)
    # A HUD measurement (riot-hud-sync) beats every automatic method and the
    # .env override: re-runs must not move clips off it.
    hud = hud_offset(cache_dir_for(video_id))
    cal = (
        OffsetResult(hud, None, HUD_SOURCE)
        if hud is not None
        else calibrate_offset(
            kills, energy, asset.get("path"), align=align_offset, verify=verify_offset
        )
    )
    offset, quality, offset_source, ocr_check = (
        cal.offset_s,
        cal.quality,
        cal.source,
        cal.ocr_check,
    )
    pad = settings.analyze_window_padding
    # riot.py built fight bounds at game clock + sync; move them to the
    # calibrated basis (game clock + calibrated + sync) shared with the anchor.
    fight_shift = offset - settings.riot_sync_offset_seconds
    # A human-measured override or HUD measurement is trusted as-is: no
    # drift second-guessing.
    wide_pre_pad = (
        None
        if offset_source in ("manual_override", HUD_SOURCE)
        else _drift_pre_pad(kills, offset, energy, duration)
    )
    try:
        record_offset(video_id, offset, source=offset_source)
    except OSError as e:
        _log.warning("Riot cache offset update skipped: %s", e)

    for r in riot_rows:
        gc = r["metadata"]["riot_event_ms"] / 1000.0
        anchor = max(0.0, min(duration, gc + offset))
        start = round(max(0.0, anchor - pad), 2)
        end = round(min(duration, anchor + pad), 2)
        meta = dict(r["metadata"])
        meta["anchor_seconds"] = round(anchor, 2)
        meta["detected_offset_seconds"] = round(offset, 1)
        meta["offset_quality"] = quality
        meta["offset_source"] = offset_source
        meta["ocr_check"] = ocr_check
        for key in _FIGHT_KEYS:
            if meta.get(key) is not None:
                meta[key] = round(meta[key] + fight_shift, 2)
        if wide_pre_pad is not None:
            meta["pre_pad_s"] = wide_pre_pad
        rows.append(
            {
                "id": str(uuid.uuid4()),
                "video_id": video_id,
                "source": "riot_api",
                "start_seconds": start,
                "end_seconds": end,
                "event_type": r["event_type"],
                "confidence": r["confidence"],
                "metadata": meta,
                "created_at": now,
            }
        )

    return rows, riot_status
