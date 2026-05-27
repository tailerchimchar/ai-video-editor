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

import uuid
from datetime import datetime, timezone

from ..config import settings
from .calibrate import align_offset
from .candidates.riot import detect_riot_events
from .ocr import verify_offset


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
    ocr_check: dict | None = None
    offset: float
    quality: float | None
    offset_source: str

    if settings.riot_offset_override_seconds is not None:
        offset = settings.riot_offset_override_seconds
        quality = None
        offset_source = "manual_override"
    else:
        offset, quality = align_offset(kills, energy)
        offset_source = "audio"
        if quality < settings.riot_offset_min_quality and kills:
            gc_ref = kills[len(kills) // 2]
            ocr_check = verify_offset(asset["path"], gc_ref + offset, gc_ref)
            if ocr_check.get("ocr_available"):
                offset = ocr_check["ocr_offset_seconds"]
                offset_source = "ocr"

    # Optional manual nudge on top of whatever the calibration produced.
    offset += settings.riot_sync_offset_seconds
    pad = settings.analyze_window_padding

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
