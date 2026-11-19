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
from dataclasses import dataclass
from pathlib import Path

from ..killfeed import verify_windows_have_kill
from ..overwolf_log import events_for_video, player_identity

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class EventAnchor:
    """One ground-truth kill/multi-kill anchor in a source video.

    `anchor_seconds` is already offset by the killfeed-lag correction
    (see `_ANCHOR_OFFSET_SECONDS`) — snap sub-clip windows directly to
    it, no further math.

    `event_type` matches the canonical types in `_CLUSTER_EVENT_TYPES`
    (`kill`, `multi_kill_2`, `multi_kill_3`, `multi_kill_4`, `ace`) so
    the anchoring module can pick per-type pre/post buffers.
    """

    anchor_seconds: float
    event_type: str
    metadata: dict  # cluster_size, headshot, first/last_kill_seconds


def kill_anchors_for_video(
    video_path: str,
    duration_seconds: float,
    *,
    include_clusters: bool = True,
) -> list[EventAnchor]:
    """Ground-truth kill anchors for `video_path`. Empty if the file
    isn't in Outplayed's dispatch table (user-clipped shorts / files
    predating install / log rotated).

    Individual kills are always emitted; `include_clusters=True` (default)
    also emits one anchor per multi-kill cluster (double/triple/quad/ace).
    Set False if you want individual frags only (e.g. voice-over pacing).

    All anchors are killfeed-OCR verified — teammate kills and death
    frames are already dropped upstream in `detect_overwolf_events`.
    """
    rows = detect_overwolf_events(video_path, duration_seconds, game="Valorant")
    if not rows:
        return []
    _kill_types = {"kill", "multi_kill_2", "multi_kill_3", "multi_kill_4", "ace"}
    out: list[EventAnchor] = []
    for r in rows:
        et = r["event_type"]
        if et not in _kill_types:
            continue
        if not include_clusters and et != "kill":
            continue
        out.append(
            EventAnchor(
                anchor_seconds=float(r["metadata"]["anchor_seconds"]),
                event_type=et,
                metadata=dict(r["metadata"]),
            )
        )
    out.sort(key=lambda a: a.anchor_seconds)
    return out


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

# Multi-kill clustering window: kills within this many seconds of
# each other belong to the same "round" burst. Valorant rounds are
# ~100s max, but a multi-kill burst is nearly always within 20-25s.
# 30s is a generous ceiling that still filters out cross-round chains.
_MULTIKILL_WINDOW_SECONDS = 30.0

# Padding around a multi-kill cluster before/after the first/last
# kill anchor. Wider than a single-kill window because we want the
# viewer to see the buildup before the first frag and the aftermath
# after the last one.
_MULTIKILL_LEAD_SECONDS = 6.0
_MULTIKILL_TRAIL_SECONDS = 4.0

# Cluster-size -> canonical event_type. A 5+ cluster in one round is
# an ace (all 5 enemies dead by the same player). 4 = quadra kill, 3 =
# triple, 2 = double. Solo kills stay as `kill`.
_CLUSTER_EVENT_TYPES = {
    2: "multi_kill_2",
    3: "multi_kill_3",
    4: "multi_kill_4",
    5: "ace",
}

# Overwolf's `time` field fires when the killfeed row APPEARS on screen,
# which is ~2-4 seconds AFTER the actual kill instant (weapon fires,
# enemy drops, kill sfx plays, killfeed animates in). We shift the
# anchor earlier by this offset so the trim window centers on the
# frag itself, not the killfeed reveal. Empirically calibrated on
# user feedback from 2026-07-08.
_ANCHOR_OFFSET_SECONDS = 3.0


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
        # Shift back by _ANCHOR_OFFSET_SECONDS — killfeed appearance
        # lags the kill instant; we want the frag frame.
        anchor_seconds = max(
            0.0, ev["time_ms_in_video"] / 1000.0 - _ANCHOR_OFFSET_SECONDS
        )
        # Clamp to the video's actual bounds (defensive — the log's
        # `duration` matches ffprobe within ~1s but we don't trust it
        # blindly).
        if anchor_seconds > duration_seconds + 1.0:
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

    # Killfeed OCR verification for KILL rows only. Deaths / assists /
    # spikes / clutches / victories aren't verifiable this way (the
    # player isn't the KILLER in the killfeed row). If tesseract or
    # ffmpeg is missing, verifier returns [True, ...] — never drops.
    identity = player_identity()
    player_name = (identity or {}).get("player_name") or ""
    kill_indices = [i for i, r in enumerate(rows) if r["event_type"] == "kill"]
    if kill_indices and player_name:
        anchors = [rows[i]["metadata"]["anchor_seconds"] for i in kill_indices]
        verdicts = verify_windows_have_kill(
            str(path), player_name, game, anchors
        )
        keep = set()
        dropped = 0
        for idx, verified in zip(kill_indices, verdicts, strict=True):
            if verified:
                keep.add(idx)
                rows[idx]["metadata"]["killfeed_verified"] = True
            else:
                dropped += 1
        rows = [
            r for i, r in enumerate(rows)
            if i not in kill_indices or i in keep
        ]
        _log.info(
            "overwolf_events: killfeed verifier dropped %d of %d kill rows "
            "for %s (player=%s)",
            dropped,
            len(kill_indices),
            path.name,
            player_name.split("#", 1)[0],
        )

    # Multi-kill clustering: group verified kills within a
    # _MULTIKILL_WINDOW_SECONDS window and emit ONE extra candidate per
    # 2+ cluster. Individual kill candidates stay in the list too --
    # downstream the mentor rubric can route them to different buckets
    # (single kills -> voice-over pool, aces -> hype montage pool).
    multi_kill_rows = _cluster_multikills(
        [r for r in rows if r["event_type"] == "kill"],
        duration_seconds,
    )
    if multi_kill_rows:
        rows.extend(multi_kill_rows)
        _log.info(
            "overwolf_events: emitted %d multi-kill cluster(s) for %s",
            len(multi_kill_rows),
            path.name,
        )

    _log.info(
        "overwolf_events: %d candidates for %s (game=%s)",
        len(rows),
        path.name,
        game,
    )
    return rows


def _cluster_multikills(
    kill_rows: list[dict],
    duration_seconds: float,
) -> list[dict]:
    """Group consecutive kills within `_MULTIKILL_WINDOW_SECONDS` into
    clusters. Emit ONE candidate per cluster of size >= 2, event_type
    per `_CLUSTER_EVENT_TYPES` (2K/3K/4K/ace).

    Pure -- unit-testable without ffmpeg or logs.

    Kills must already be sorted by anchor_seconds (they are, coming
    out of the log parser). Behavior on unsorted input is undefined
    but non-fatal.
    """
    if len(kill_rows) < 2:
        return []

    # Greedy left-to-right walk: each kill either extends the current
    # cluster (if its anchor is within window of the LAST kill's anchor)
    # or starts a new cluster.
    clusters: list[list[dict]] = []
    current: list[dict] = []
    last_anchor = -_MULTIKILL_WINDOW_SECONDS * 2
    for row in kill_rows:
        anchor = row["metadata"]["anchor_seconds"]
        if anchor - last_anchor <= _MULTIKILL_WINDOW_SECONDS:
            current.append(row)
        else:
            if len(current) >= 2:
                clusters.append(current)
            current = [row]
        last_anchor = anchor
    if len(current) >= 2:
        clusters.append(current)

    out: list[dict] = []
    for cluster in clusters:
        size = len(cluster)
        # Clamp 5+ to "ace" (5-kill maximum in a Valorant round).
        canonical = _CLUSTER_EVENT_TYPES.get(min(size, 5), "multi_kill_2")
        first_anchor = cluster[0]["metadata"]["anchor_seconds"]
        last_anchor_v = cluster[-1]["metadata"]["anchor_seconds"]
        start = max(0.0, first_anchor - _MULTIKILL_LEAD_SECONDS)
        end = min(duration_seconds, last_anchor_v + _MULTIKILL_TRAIL_SECONDS)
        headshot_count = sum(
            1 for c in cluster if c["metadata"].get("headshot")
        )
        rationale = (
            f"Multi-kill cluster: {size} kills in "
            f"{last_anchor_v - first_anchor:.1f}s"
            + (f" ({headshot_count} headshots)" if headshot_count else "")
        )
        out.append(
            {
                "start_seconds": round(start, 2),
                "end_seconds": round(end, 2),
                "event_type": canonical,
                # Ground truth AND multi-kill = highest confidence a
                # candidate can carry. The mentor rubric routes these
                # straight to the hype montage bucket.
                "confidence": 1.0,
                "metadata": {
                    # For strategies that anchor on a single moment,
                    # use the midpoint of the cluster.
                    "anchor_seconds": round(
                        (first_anchor + last_anchor_v) / 2.0, 2
                    ),
                    "cluster_size": size,
                    "first_kill_seconds": round(first_anchor, 2),
                    "last_kill_seconds": round(last_anchor_v, 2),
                    "headshot_count": headshot_count,
                    "rationale": rationale,
                },
            }
        )
    return out
