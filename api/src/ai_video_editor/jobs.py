"""Single job-completion helper: writes the jobs row, then emits `job.done`."""

from __future__ import annotations

import contextlib
from datetime import datetime, timezone

from .notify import emit
from .notify.models import Event

_SCALAR_KEYS = (
    "folder", "clips_written", "clips_total", "shorts_written", "shorts_total",
    "dropped", "game", "champion",
)
_ITEM_KEYS = {
    "clips": ("file", "event", "start_seconds", "end_seconds", "vlm_verdict"),
    "shorts": ("file", "bucket", "title", "vlm_verdict"),
}


def summarize(result: dict | None) -> dict:
    """Whitelist a job result for outbound events; never free text."""
    if not result:
        return {}
    out = {k: result[k] for k in _SCALAR_KEYS if k in result}
    for name, keys in _ITEM_KEYS.items():
        items = result.get(name)
        if isinstance(items, list):
            out[name] = [
                {k: it[k] for k in keys if k in it} for it in items if isinstance(it, dict)
            ]
    return out


async def finish_job(
    db,
    job_id: str,
    *,
    output_path: str | None = None,
    error: str | None = None,
    job_type: str | None = None,
    result: dict | None = None,
) -> None:
    """Best-effort: a failing DB write must not crash the task; always emit."""
    status = "failed" if error else "completed"
    with contextlib.suppress(Exception):
        await db.execute(
            "UPDATE jobs SET status = ?, output_path = ?, error = ?, completed_at = ? "
            "WHERE id = ?",
            (status, output_path, error, datetime.now(timezone.utc).isoformat(), job_id),
        )
        await db.commit()
    emit(
        Event(
            event="job.done",
            payload={
                "job_id": job_id,
                "job_type": job_type,
                "status": status,
                "output_path": output_path,
                "error": error,
                "summary": summarize(result),
            },
        )
    )
