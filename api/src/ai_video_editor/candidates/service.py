"""Candidate generation service.

Pure/sync: given a video, produce HighlightCandidate rows from every
available source. DB persistence is handled by the caller (router) so
this stays testable and offloadable via asyncio.to_thread.

Source files are never modified — we only read/probe them.
"""

import uuid
from datetime import datetime, timezone

from ..config import settings
from ..league import is_league
from ..league.orchestrator import gather_lol_candidates
from .audio import energy_curve, peaks_from_energy
from .probe import get_duration_seconds
from .transcript import detect_transcript_keywords


def compute_candidates(
    asset: dict, transcript_segments: list[dict] | None = None
) -> tuple[list[dict], dict]:
    """Return (rows, diagnostics) for one video.

    `diagnostics` carries non-fatal source status, e.g.
    {"riot": "rate_limited"} — so the job can tell an empty Riot result
    caused by throttling from a genuine "no confident match".

    `asset` is an assets-table row (id, filename, path, game, created_at).

    Strategy:
      - Short file (<= cutoff): Outplayed already cut it as an event clip,
        so the whole file is one high-confidence candidate.
      - Long file: it's a full session recording — fall back to audio
        peaks to find candidate moments.
      - LoL recordings: dispatch to `league.orchestrator` for the Riot
        kill timeline + per-recording offset calibration. Other games
        skip this entirely (no upward imports from `league/`).
      - Transcript keywords: always attempted (cross-game).
    """
    video_id = asset["id"]
    video_path = asset["path"]
    now = datetime.now(timezone.utc).isoformat()
    duration = get_duration_seconds(video_path)
    rows: list[dict] = []

    def _row(
        source: str, start: float, end: float, event: str | None, conf: float | None, meta: dict
    ) -> dict:
        return {
            "id": str(uuid.uuid4()),
            "video_id": video_id,
            "source": source,
            "start_seconds": start,
            "end_seconds": end,
            "event_type": event,
            "confidence": conf,
            "metadata": meta,
            "created_at": now,
        }

    if 0 < duration <= settings.outplayed_clip_max_seconds:
        rows.append(
            _row(
                "outplayed_clip",
                0.0,
                round(duration, 2),
                "unknown",
                0.9,
                {
                    "duration_seconds": round(duration, 2),
                    "rationale": "short file -> Outplayed-generated event clip",
                },
            )
        )
    # Decode the recording's audio once; reused for loud-region
    # candidates AND to calibrate the Riot offset per-recording.
    energy = None
    if duration > settings.outplayed_clip_max_seconds:
        energy = energy_curve(video_path, duration)
        for peak in peaks_from_energy(energy):
            rows.append(
                _row(
                    "audio_peak",
                    peak["start_seconds"],
                    peak["end_seconds"],
                    "funny_audio",
                    peak["confidence"],
                    {
                        "duration_seconds": round(duration, 2),
                        "rationale": "loud region in full recording",
                    },
                )
            )

    # LoL-only block: Riot kill timeline + per-recording offset
    # calibration. Dispatched to `league/` so non-LoL paths don't import
    # any of it; LoL paths get the full per-recording offset machinery.
    if is_league(asset.get("game")):
        lol_rows, riot_status = gather_lol_candidates(asset, duration, energy)
        rows.extend(lol_rows)
    else:
        riot_status = "disabled"

    for kw in detect_transcript_keywords(transcript_segments or [], asset.get("game")):
        rows.append(
            _row(
                "transcript_keyword",
                kw["start_seconds"],
                kw["end_seconds"],
                kw.get("event_type"),
                kw.get("confidence"),
                kw.get("metadata", {}),
            )
        )

    return rows, {"riot": riot_status}
