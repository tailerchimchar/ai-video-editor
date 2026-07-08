"""Overwolf-events candidate source — ground-truth kill/death/assist
events read straight out of Outplayed's log.

This is the highest-quality candidate source we have for anything
Outplayed dispatched (see `overwolf_log.py`). Every kill, death,
assist, headshot, spike interaction, and victory that fires in-game
gets logged with its offset in the video's own timeline. Zero OCR,
zero VLM, zero API.

**Only fires when Outplayed dispatched THIS specific video.** User-
manually-clipped shorts don't get dispatch entries; those fall
through to OCR + VLM. Game-agnostic — Overwolf supports many games
via per-game event providers (Val is the current one Outplayed
subscribes to).
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..overwolf_log import events_for_video

_log = logging.getLogger(__name__)


# Overwolf event types we surface as candidates. Others get logged but
# not surfaced (they're rarely useful for highlights).
_SURFACE_TYPES = frozenset(
    {
        "kill",
        "death",
        "assist",
        "clutch",
        "spike_planted",
        "spike_defused",
        "spike_detonated",
        "victory",
    }
)

# Event type → canonical name we emit as `event_type` on the candidate.
# `headshot` deliberately isn't in this map — it fires alongside `kill`
# at the same time_ms; we dedupe and promote via metadata instead of
# emitting a duplicate candidate.
_TYPE_MAP: dict[str, str] = {
    "kill": "kill",
    "death": "death",
    "assist": "assist",
    "clutch": "clutch",
    "spike_planted": "objective",
    "spike_defused": "objective",
    "spike_detonated": "objective",
    "victory": "victory",
}

# Anchor ± window seconds for the candidate cut. Matches the LoL Riot
# candidate window (±5s) so downstream widening rules apply the same
# way.
_WINDOW_HALF_SECONDS = 5.0


def detect_overwolf_events(
    video_path: str,
    duration_seconds: float,
    game: str | None = None,
) -> list[dict]:
    """Return one candidate row per surfaced Overwolf event for
    `video_path`. Empty list when the file isn't in Outplayed's
    dispatch table (user-clipped shorts / games without Overwolf
    events / files predating Overwolf install).

    Never raises: log-read errors and missing paths return `[]`.
    """
    path = Path(video_path)
    events = events_for_video(path, event_types=tuple(_SURFACE_TYPES))
    if events is None:
        # Not in dispatch table — that's fine. Other sources cover it.
        return []
    if not events:
        return []

    # Index headshot times so we can flag corresponding kill events
    all_events_for_headshot = events_for_video(path, event_types=("headshot",))
    headshot_times_ms = (
        {e["time_ms_in_video"] for e in all_events_for_headshot}
        if all_events_for_headshot
        else set()
    )

    rows: list[dict] = []
    for ev in events:
        etype = ev["type"]
        canonical = _TYPE_MAP.get(etype)
        if canonical is None:
            continue
        anchor_seconds = ev["time_ms_in_video"] / 1000.0
        # Clamp to the video's actual bounds (defensive — the log's
        # `duration` matches ffprobe within ~1s but we don't trust it
        # blindly).
        if anchor_seconds < 0 or anchor_seconds > duration_seconds + 1.0:
            continue
        start = max(0.0, anchor_seconds - _WINDOW_HALF_SECONDS)
        end = min(duration_seconds, anchor_seconds + _WINDOW_HALF_SECONDS)
        is_headshot = ev["time_ms_in_video"] in headshot_times_ms
        rationale = (
            f"Overwolf event: {etype}"
            + (" (headshot)" if is_headshot and etype == "kill" else "")
            + (f" #{ev['data']}" if ev.get("data") else "")
        )
        rows.append(
            {
                "start_seconds": round(start, 2),
                "end_seconds": round(end, 2),
                "event_type": canonical,
                # 1.0 = ground truth from the game client. Nothing else
                # in the pipeline gets to claim this level of certainty.
                "confidence": 1.0,
                "metadata": {
                    "anchor_seconds": round(anchor_seconds, 2),
                    "overwolf_type": etype,
                    "overwolf_data": ev.get("data", ""),
                    "headshot": is_headshot,
                    "rationale": rationale,
                },
            }
        )

    _log.info(
        "overwolf_events: %d candidates for %s (game=%s)",
        len(rows),
        path.name,
        game,
    )
    return rows
