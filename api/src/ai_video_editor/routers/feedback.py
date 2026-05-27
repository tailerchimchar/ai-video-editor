"""User-edit feedback read endpoints.

Write-side lives in `feedback.log_event` and is called inline from each
mutating edit endpoint. This module is the READ surface: aggregates +
proposed-window summaries the user can inspect to decide whether to
apply learned defaults.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..database import get_db
from ..feedback import summarise

router = APIRouter(tags=["feedback"], prefix="/feedback")


@router.get("/summary")
async def feedback_summary(compilation_id: str | None = None) -> dict:
    """Aggregated user-edit signal.

    Optionally filter to one compilation. Returns per-action counts,
    per-event-type counts, extend-deltas medians per event_type, and
    a `proposed_event_window_overrides` dict you could (manually) drop
    into `.env` to update the per-event defaults.

    The system intentionally does NOT auto-apply the proposals — review
    the medians first; one user with quirky taste shouldn't reshape the
    global defaults after a few edits.
    """
    db = await get_db()
    try:
        return await summarise(db, compilation_id=compilation_id)
    except Exception as exc:  # pragma: no cover — best-effort read
        raise HTTPException(500, f"feedback summary failed: {exc}") from exc
    finally:
        await db.close()
