"""One place that turns Riot game-clock times into recording seconds.

The candidate path (`orchestrator.gather_lol_candidates`) and the timeline
fetch (`candidates.riot.fetch_timeline_inputs`) must agree on the offset,
or timeline events and clips land on different seconds. Both call
`calibrate_offset`.

Order: human override (authoritative) -> audio alignment of the kill comb
against the recording's loudness -> OCR clock read when the audio lock is
weak. `RIOT_SYNC_OFFSET_SECONDS` is added on top in every case.
`sync_only_offset` is the fallback when there is no recording to calibrate
against.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ..config import settings
from .calibrate import align_offset
from .ocr import verify_offset

SYNC_ONLY = "sync_only"


@dataclass
class OffsetResult:
    offset_s: float  # final: add to a game-clock time to get recording seconds
    quality: float | None
    source: str  # manual_override | audio | ocr | sync_only
    ocr_check: dict | None = None


def calibrate_offset(
    kills: list[float],
    energy,
    video_path: str | None,
    *,
    align: Callable | None = None,
    verify: Callable | None = None,
) -> OffsetResult:
    """`kills`: game-clock seconds of the owner's Riot events (sorted).
    `align` / `verify` default to `calibrate.align_offset` / `ocr.verify_offset`."""
    align = align or align_offset
    verify = verify or verify_offset
    ocr_check: dict | None = None
    if settings.riot_offset_override_seconds is not None:
        offset = float(settings.riot_offset_override_seconds)
        quality: float | None = None
        source = "manual_override"
    else:
        offset, quality = align(kills, energy)
        source = "audio"
        if quality < settings.riot_offset_min_quality and kills and video_path:
            gc_ref = kills[len(kills) // 2]
            ocr_check = verify(video_path, gc_ref + offset, gc_ref)
            if ocr_check.get("ocr_available"):
                offset = ocr_check["ocr_offset_seconds"]
                source = "ocr"
    return OffsetResult(offset + settings.riot_sync_offset_seconds, quality, source, ocr_check)


def sync_only_offset() -> OffsetResult:
    return OffsetResult(float(settings.riot_sync_offset_seconds), None, SYNC_ONLY)
