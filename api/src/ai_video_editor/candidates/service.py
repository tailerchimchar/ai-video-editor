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
from .cv_kda import detect_kda_events
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
                    # Honest label — this is JUST a loud audio moment.
                    # The `promote_audio_event_types` pass upgrades it to
                    # `kill` / `death` / `assist` when a nearby visible
                    # event fires. The historical `funny_audio` label
                    # incorrectly implied humor content the finder never
                    # verified.
                    "audio_peak",
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

    # CV-based scoreboard OCR (cross-game — needs the game profile to
    # declare a `scoreboard` region). Only runs on session recordings
    # (longer than outplayed_clip_max_seconds) because Outplayed clips
    # are too short to benefit + the existing outplayed_clip source
    # already covers them.
    for kda in detect_kda_events(video_path, duration, asset.get("game")):
        rows.append(
            _row(
                "cv_kda",
                kda["start_seconds"],
                kda["end_seconds"],
                kda.get("event_type"),
                kda.get("confidence"),
                kda.get("metadata", {}),
            )
        )

    # Promote event_type on audio-derived candidates when a visible
    # kill/death/assist happened nearby. Without this, audio_peak clips
    # get shipped as event_type='audio_peak' even when they anticipate
    # or celebrate a real Riot/cv_kda kill — which then confuses the VLM
    # ("I can't verify an audio event from silent frames"). The fix
    # belongs here, not in the ranker or the VLM prompt: the candidate
    # label should match the visible truth.
    rows = promote_audio_event_types(rows)

    return rows, {"riot": riot_status}


_AUDIO_SOURCES = frozenset({"audio_peak", "transcript_keyword"})
_VISIBLE_SOURCES = frozenset({"riot_api", "cv_kda"})
_VISIBLE_EVENTS = frozenset({"kill", "death", "assist", "ace", "pentakill",
                             "quadrakill", "doublekill", "teamfight",
                             "baron", "dragon", "objective"})


# Seconds of slack around a visible event for the audio promoter to
# still count as "same moment". A loud audio peak that anticipates OR
# celebrates a kill often lands 10-20 seconds away from the kill's
# exact timestamp; strict overlap misses those. The `cluster_gap_seconds`
# (30s) sets the ranker's cluster boundary; we use a smaller value
# here so distant events don't wrongly promote each other.
_AUDIO_PROMOTION_TOLERANCE_SECONDS = 20.0


def promote_audio_event_types(
    rows: list[dict],
    *,
    tolerance_seconds: float = _AUDIO_PROMOTION_TOLERANCE_SECONDS,
) -> list[dict]:
    """Replace audio-derived `event_type` with the visible event that
    fires nearby (within `tolerance_seconds`).

    Pure: consumes and returns candidate rows only, no I/O. Written as a
    top-level helper so it's unit-testable without spinning up the whole
    `compute_candidates` pipeline. Deterministic ordering — same input →
    same output.

    Rule:
      - Only touch rows with `source in _AUDIO_SOURCES` (audio_peak +
        transcript_keyword)
      - Look for any row with `source in _VISIBLE_SOURCES` whose
        window is within `tolerance_seconds` of the audio row's window
        (proximity, not strict overlap — a loud reaction can precede
        or celebrate a kill by ~15s and still be the same moment)
      - If found, and the visible row's `event_type` is a real gameplay
        event (kill / death / assist / etc), copy that `event_type`
        onto the audio row and stash `metadata.promoted_from` so the
        trace still shows what fired originally
      - Otherwise leave the audio row alone (there's no visible
        counterpart, so `audio_peak` is honest)

    We do NOT drop the audio row — its confidence + timing carry the
    audio signal, which the ranker still wants as a boost. Only the
    label changes.
    """
    visible = [
        r
        for r in rows
        if r.get("source") in _VISIBLE_SOURCES
        and r.get("event_type") in _VISIBLE_EVENTS
    ]
    if not visible:
        return rows

    out: list[dict] = []
    for r in rows:
        if r.get("source") not in _AUDIO_SOURCES:
            out.append(r)
            continue
        a_start = float(r.get("start_seconds", 0.0))
        a_end = float(r.get("end_seconds", 0.0))
        best: dict | None = None
        best_conf = -1.0
        for v in visible:
            v_start = float(v.get("start_seconds", 0.0))
            v_end = float(v.get("end_seconds", 0.0))
            # Proximity within tolerance — the gap between the two
            # intervals must be <= tolerance_seconds. Strict overlap
            # is a subset (gap = 0).
            gap = max(0.0, max(v_start - a_end, a_start - v_end))
            if gap > tolerance_seconds:
                continue
            conf = float(v.get("confidence") or 0.0)
            if conf > best_conf:
                best_conf = conf
                best = v
        if best is None:
            out.append(r)
            continue
        # Copy — preserve `metadata.promoted_from` for trace + testability
        promoted = dict(r)
        promoted["metadata"] = {
            **(r.get("metadata") or {}),
            "promoted_from": r.get("event_type"),
            "promoted_by_source": best.get("source"),
        }
        promoted["event_type"] = best.get("event_type")
        out.append(promoted)
    return out
