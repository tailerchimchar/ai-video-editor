"""SFX router (Sprint #2).

POST /sfx/extract — cut an audio span out of an indexed asset into a
clean wav under `WORKSPACE/media_library/<game>/sfx/<file>`. Used to
source the per-game audio templates that the profile names (and that
sprint #3's `add_sfx` overlay + sprint #4's audio-event detector will
consume).

Thin HTTP shell over `sfx.extract_sfx`. Job-based like the rest of the
app — poll `/jobs/{id}`.
"""

import asyncio
import contextlib
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..config import settings
from ..database import get_db
from ..models import RankResponse
from ..profiles import load_profile
from ..sfx import extract_sfx

router = APIRouter(tags=["sfx"], prefix="/sfx")

_background_tasks: set[asyncio.Task] = set()


def _track(coro) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def _finish(db, job_id: str, *, output_path: str | None, error: str | None) -> None:
    with contextlib.suppress(Exception):
        status = "failed" if error else "completed"
        await db.execute(
            "UPDATE jobs SET status = ?, output_path = ?, error = ?, completed_at = ? WHERE id = ?",
            (status, output_path, error, datetime.now(timezone.utc).isoformat(), job_id),
        )
        await db.commit()


class ExtractSfxRequest(BaseModel):
    asset_id: str
    game: str
    sound_name: str = Field(..., min_length=1, max_length=64)
    start_seconds: float = Field(..., ge=0.0)
    end_seconds: float = Field(..., gt=0.0)


def _resolve_output_path(game: str, sound_name: str) -> tuple[str, str | None]:
    """Where the extracted .wav goes, plus an optional warning when
    `sound_name` isn't declared in the profile.

    Uses `profile.meta.game` (canonical) for the directory so aliases
    all land in one place. If the sound IS declared, its `file` value
    is the basename (so the file matches what `add_sfx`/the detector
    expect). If not declared, falls back to `<sound_name>.wav` — and
    the caller surfaces a warning so the user knows to add the entry
    to the profile (or stay out-of-band).
    """
    profile = load_profile(game)
    sound = profile.sounds.get(sound_name)
    if sound is not None:
        basename = sound.file
        warning = None
    else:
        basename = f"{sound_name}.wav"
        warning = (
            f"sound '{sound_name}' is not declared in profile '{profile.meta.game}' — "
            f"saved as {basename} but won't be picked up until you add it to the profile"
        )
    folder = settings.workspace_dir / "media_library" / profile.meta.game / "sfx"
    return (folder / basename).as_posix(), warning


async def _run_extract_job(
    job_id: str,
    asset: dict,
    req: ExtractSfxRequest,
    output_path: str,
    warning: str | None,
) -> None:
    db = await get_db()
    try:
        await db.execute("UPDATE jobs SET status = 'running' WHERE id = ?", (job_id,))
        await db.commit()
        ok, err = await asyncio.to_thread(
            extract_sfx, asset["path"], output_path, req.start_seconds, req.end_seconds
        )
        if not ok:
            await _finish(db, job_id, output_path=None, error=err)
            return
        # Surface the "sound not declared" warning in the output_path
        # field so callers polling /jobs/{id} see it without a separate
        # field — same channel as the success path message.
        msg = f"{output_path}" + (f"  [warning: {warning}]" if warning else "")
        await _finish(db, job_id, output_path=msg, error=None)
    except Exception as e:
        await _finish(db, job_id, output_path=None, error=str(e)[:2000])
    finally:
        await db.close()


@router.post("/extract", response_model=RankResponse)
async def extract(req: ExtractSfxRequest):
    if req.end_seconds <= req.start_seconds:
        raise HTTPException(400, "end_seconds must be greater than start_seconds")

    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT * FROM assets WHERE id = ?", (req.asset_id,))
        if not rows:
            raise HTTPException(status_code=404, detail="Asset not found")
        asset = dict(rows[0])
        job_id = str(uuid.uuid4())
        await db.execute(
            "INSERT INTO jobs (id, project_id, type, status, created_at) "
            "VALUES (?, NULL, 'sfx_extract', 'pending', ?)",
            (job_id, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()
    finally:
        await db.close()

    output_path, warning = _resolve_output_path(req.game, req.sound_name)
    _track(_run_extract_job(job_id, asset, req, output_path, warning))
    return RankResponse(job_id=job_id)
