import asyncio
import subprocess
import tempfile
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from ..config import settings
from ..database import get_db
from ..models import (
    ProjectCreate,
    ProjectOut,
    RenderResponse,
    TimelineItemCreate,
    TimelineItemOut,
)

router = APIRouter(tags=["projects"])

_background_tasks: set[asyncio.Task] = set()


@router.post("/projects", response_model=ProjectOut)
async def create_project(body: ProjectCreate):
    db = await get_db()
    try:
        project_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        await db.execute(
            "INSERT INTO projects (id, name, created_at) VALUES (?, ?, ?)",
            (project_id, body.name, now),
        )
        await db.commit()
        return ProjectOut(id=project_id, name=body.name, created_at=now)
    finally:
        await db.close()


@router.post("/projects/{project_id}/timeline/items", response_model=TimelineItemOut)
async def add_timeline_item(project_id: str, body: TimelineItemCreate):
    db = await get_db()
    try:
        # Verify project exists
        rows = await db.execute_fetchall("SELECT id FROM projects WHERE id = ?", (project_id,))
        if not rows:
            raise HTTPException(status_code=404, detail="Project not found")

        # Verify clip exists
        rows = await db.execute_fetchall("SELECT id FROM clips WHERE id = ?", (body.clip_id,))
        if not rows:
            raise HTTPException(status_code=404, detail="Clip not found")

        item_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        await db.execute(
            "INSERT INTO timeline_items (id, project_id, clip_id, position, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (item_id, project_id, body.clip_id, body.position, now),
        )
        await db.commit()
        return TimelineItemOut(
            id=item_id,
            project_id=project_id,
            clip_id=body.clip_id,
            position=body.position,
            created_at=now,
        )
    finally:
        await db.close()


async def _run_render_job(job_id: str, project_id: str):
    db = await get_db()
    try:
        await db.execute("UPDATE jobs SET status = 'running' WHERE id = ?", (job_id,))
        await db.commit()

        # Get timeline clips in order
        rows = await db.execute_fetchall(
            "SELECT c.output_path FROM timeline_items ti "
            "JOIN clips c ON ti.clip_id = c.id "
            "WHERE ti.project_id = ? ORDER BY ti.position",
            (project_id,),
        )
        if not rows:
            sql = (
                "UPDATE jobs SET status = 'failed', error = 'No clips in timeline',"
                " completed_at = ? WHERE id = ?"
            )
            await db.execute(sql, (datetime.now(timezone.utc).isoformat(), job_id))
            await db.commit()
            return

        renders_dir = settings.workspace_dir / "renders"
        renders_dir.mkdir(parents=True, exist_ok=True)
        output_path = (renders_dir / f"{project_id}_rough_cut.mp4").as_posix()

        # Write concat file list
        concat_content = "\n".join(f"file '{row[0]}'" for row in rows)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write(concat_content)
            concat_file = f.name

        cmd = [
            settings.ffmpeg_path,
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            concat_file,
            "-c",
            "copy",
            output_path,
        ]

        try:
            result = await asyncio.to_thread(subprocess.run, cmd, capture_output=True, text=True)
        except FileNotFoundError:
            await db.execute(
                "UPDATE jobs SET status = 'failed', error = ?, completed_at = ? WHERE id = ?",
                (
                    "ffmpeg not found — install it: winget install Gyan.FFmpeg",
                    datetime.now(timezone.utc).isoformat(),
                    job_id,
                ),
            )
            await db.commit()
            return

        if result.returncode != 0:
            await db.execute(
                "UPDATE jobs SET status = 'failed', error = ?, completed_at = ? WHERE id = ?",
                (result.stderr[:2000], datetime.now(timezone.utc).isoformat(), job_id),
            )
        else:
            sql = (
                "UPDATE jobs SET status = 'completed', output_path = ?,"
                " completed_at = ? WHERE id = ?"
            )
            await db.execute(sql, (output_path, datetime.now(timezone.utc).isoformat(), job_id))
        await db.commit()
    finally:
        await db.close()


@router.post("/projects/{project_id}/render", response_model=RenderResponse)
async def render_project(project_id: str):
    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT id FROM projects WHERE id = ?", (project_id,))
        if not rows:
            raise HTTPException(status_code=404, detail="Project not found")

        job_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        await db.execute(
            "INSERT INTO jobs (id, project_id, type, status, created_at) "
            "VALUES (?, ?, 'render', 'pending', ?)",
            (job_id, project_id, now),
        )
        await db.commit()
    finally:
        await db.close()

    task = asyncio.create_task(_run_render_job(job_id, project_id))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return RenderResponse(job_id=job_id)
