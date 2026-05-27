"""User-edit feedback capture + summary.

Every time the user manually adjusts the system's output (extends a clip,
removes one, reverts), we log a row to `feedback_events`. The goal is to
turn user behavior into training signal:

- **Extend deltas** per event_type → tune `settings.event_window_overrides`.
  If users consistently extend `funny_audio` by +2s post, the next compile
  defaults can shift to give those clips more breathing room automatically.
- **Removes** → negative signal for the ranker. A clip the LLM kept but
  the user removed = ranker over-rated this kind of candidate.
- **Reverts** → strong negative signal on the change being reverted.

This module is the capture + read layer ONLY. Apply/recompute (writing
back to `event_window_overrides` or a ranker_bias.json) is intentionally
not implemented yet — we want to accumulate real data first before
deciding the right transform shape.

Best-effort: a logging failure here MUST NOT fail the edit. The user's
edit already succeeded by the time we're called; losing one feedback
row is annoying, breaking the edit because of it is unacceptable.
"""

from __future__ import annotations

import contextlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

import aiosqlite


async def log_event(
    db: aiosqlite.Connection,
    *,
    compilation_id: str,
    action: str,
    clip_id: str | None = None,
    event_type: str | None = None,
    delta_before: float | None = None,
    delta_after: float | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    """Record one user-edit event. Best-effort — never raises."""
    row = (
        str(uuid.uuid4()),
        compilation_id,
        clip_id,
        action,
        event_type,
        delta_before,
        delta_after,
        json.dumps(payload, default=str) if payload else None,
        datetime.now(timezone.utc).isoformat(),
    )
    with contextlib.suppress(Exception):
        await db.execute(
            "INSERT INTO feedback_events "
            "(id, compilation_id, clip_id, action, event_type, "
            " delta_before, delta_after, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            row,
        )
        await db.commit()


async def summarise(
    db: aiosqlite.Connection,
    *,
    compilation_id: str | None = None,
) -> dict[str, Any]:
    """Aggregate stats: per-event-type extension medians + action counts.

    Returns a dict shaped for the GET /feedback/summary endpoint:
        {
          "total_events": int,
          "by_action": {action: count, ...},
          "by_event_type": {event_type: count, ...},
          "extend_medians_per_event_type": {
              event_type: {"before": median_seconds, "after": median_seconds,
                           "n_samples": int}
          },
          "proposed_event_window_overrides": {
              event_type: [median_pre, median_post]
          }
        }

    `proposed_event_window_overrides` is the candidate update for
    `settings.event_window_overrides` if we trusted the medians directly.
    For now this is advisory — the recompute/apply step is intentionally
    not wired so we can review the proposed values before applying.
    """
    where = "WHERE compilation_id = ?" if compilation_id else ""
    args = (compilation_id,) if compilation_id else ()

    total_row = await db.execute_fetchall(
        f"SELECT COUNT(*) AS c FROM feedback_events {where}", args
    )
    total = dict(total_row[0])["c"] if total_row else 0

    by_action_rows = await db.execute_fetchall(
        f"SELECT action, COUNT(*) AS c FROM feedback_events {where} GROUP BY action",
        args,
    )
    by_action = {dict(r)["action"]: dict(r)["c"] for r in by_action_rows}

    by_event_rows = await db.execute_fetchall(
        f"SELECT event_type, COUNT(*) AS c FROM feedback_events "
        f"{where + (' AND ' if where else 'WHERE ')}event_type IS NOT NULL "
        f"GROUP BY event_type",
        args,
    )
    by_event = {dict(r)["event_type"]: dict(r)["c"] for r in by_event_rows}

    # Per-event-type extend deltas — list of (before, after) tuples
    extend_rows = await db.execute_fetchall(
        f"SELECT event_type, delta_before, delta_after FROM feedback_events "
        f"{where + (' AND ' if where else 'WHERE ')}action = 'extend' "
        f"AND event_type IS NOT NULL",
        args,
    )

    medians: dict[str, dict[str, float | int]] = {}
    proposed: dict[str, list[float]] = {}
    by_evt: dict[str, list[tuple[float, float]]] = {}
    for row in extend_rows:
        r = dict(row)
        et = r["event_type"]
        before = float(r["delta_before"] or 0.0)
        after = float(r["delta_after"] or 0.0)
        by_evt.setdefault(et, []).append((before, after))

    for et, deltas in by_evt.items():
        n = len(deltas)
        med_before = _median([d[0] for d in deltas])
        med_after = _median([d[1] for d in deltas])
        medians[et] = {
            "before": round(med_before, 2),
            "after": round(med_after, 2),
            "n_samples": n,
        }
        # Only propose new overrides when we have at least 3 samples to
        # avoid one-shot edits hijacking the defaults.
        if n >= 3:
            proposed[et] = [round(med_before, 2), round(med_after, 2)]

    return {
        "total_events": total,
        "by_action": by_action,
        "by_event_type": by_event,
        "extend_medians_per_event_type": medians,
        "proposed_event_window_overrides": proposed,
    }


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    mid = len(s) // 2
    if len(s) % 2:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2.0
