import asyncio
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from ..config import settings
from ..database import get_db
from ..editing import trim_clip
from ..models import ClipCreate, ClipOut

router = APIRouter(tags=["clips"])

_background_tasks: set[asyncio.Task] = set()  # prevent GC of background tasks


async def _run_clip_job(job_id: str, asset_path: str, output_path: str, start: float, end: float):
    db = await get_db()
    try:
        await db.execute("UPDATE jobs SET status = 'running' WHERE id = ?", (job_id,))
        await db.commit()

        ok, error = await asyncio.to_thread(trim_clip, asset_path, output_path, start, end)
        now = datetime.now(timezone.utc).isoformat()
        if not ok:
            await db.execute(
                "UPDATE jobs SET status = 'failed', error = ?, completed_at = ? WHERE id = ?",
                (error, now, job_id),
            )
        else:
            await db.execute(
                "UPDATE jobs SET status = 'completed', output_path = ?,"
                " completed_at = ? WHERE id = ?",
                (output_path, now, job_id),
            )
        await db.commit()
    finally:
        await db.close()


@router.post("/clips", response_model=ClipOut)
async def create_clip(body: ClipCreate):
    if body.start_seconds >= body.end_seconds:
        raise HTTPException(status_code=400, detail="start_seconds must be less than end_seconds")
    if body.start_seconds < 0:
        raise HTTPException(status_code=400, detail="start_seconds must be non-negative")

    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT * FROM assets WHERE id = ?", (body.asset_id,))
        if not rows:
            raise HTTPException(status_code=404, detail="Asset not found")
        asset = dict(rows[0])

        clip_id = str(uuid.uuid4())
        job_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        clips_dir = settings.workspace_dir / "clips"
        clips_dir.mkdir(parents=True, exist_ok=True)
        output_path = (clips_dir / f"{clip_id}.mp4").as_posix()

        await db.execute(
            "INSERT INTO clips (id, asset_id, start_seconds, end_seconds, output_path, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (clip_id, body.asset_id, body.start_seconds, body.end_seconds, output_path, now),
        )
        await db.execute(
            "INSERT INTO jobs (id, project_id, type, status, created_at) "
            "VALUES (?, NULL, 'clip', 'pending', ?)",
            (job_id, now),
        )
        await db.commit()
    finally:
        await db.close()

    task = asyncio.create_task(
        _run_clip_job(job_id, asset["path"], output_path, body.start_seconds, body.end_seconds)
    )
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    return ClipOut(
        id=clip_id,
        asset_id=body.asset_id,
        start_seconds=body.start_seconds,
        end_seconds=body.end_seconds,
        output_path=output_path,
        created_at=now,
        job_id=job_id,
    )
