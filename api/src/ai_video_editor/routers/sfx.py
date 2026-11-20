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
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..config import settings
from ..database import get_db
from ..edits import apply_sfx
from ..jobs import finish_job
from ..models import RankResponse
from ..profiles import load_profile
from ..sfx import extract_sfx

router = APIRouter(tags=["sfx"], prefix="/sfx")

_background_tasks: set[asyncio.Task] = set()


def _track(coro) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


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
            await finish_job(db, job_id, output_path=None, error=err)
            return
        # Surface the "sound not declared" warning in the output_path
        # field so callers polling /jobs/{id} see it without a separate
        # field — same channel as the success path message.
        msg = f"{output_path}" + (f"  [warning: {warning}]" if warning else "")
        await finish_job(db, job_id, output_path=msg, error=None)
    except Exception as e:
        await finish_job(db, job_id, output_path=None, error=str(e)[:2000])
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


# ---------------------------------------------------------------------
# Overlay one or more SFX one-shots onto a rendered clip. Path-based --
# reuse the trim/caption sanitizer pattern for output naming.
# ---------------------------------------------------------------------


class SfxHitIn(BaseModel):
    """One SFX one-shot in the overlay stack."""

    sfx_path: str = Field(..., description="Absolute path to a .wav/.mp3.")
    at_seconds: float = Field(..., ge=0.0)
    volume: float = Field(default=1.0, ge=0.0, le=4.0)


class SfxOverlayBody(BaseModel):
    source_path: str = Field(..., description="Absolute path to the input .mp4.")
    sfx: list[SfxHitIn] = Field(..., min_length=1)
    name: str | None = Field(default=None)


_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_\-]+")


def _sanitize(name: str) -> str:
    return _SAFE_NAME_RE.sub("_", name).strip("_") or "sfx"


@router.post("/overlay")
async def sfx_overlay(body: SfxOverlayBody):
    """Layer SFX one-shots on top of any .mp4's audio track. Sync --
    returns the output path when the encode finishes.

    Chain with `trim_source_to_clip` and `compile_custom_montage` to
    build the classic 'joke lands + vine boom' TikTok gaming edit.
    """
    src = Path(body.source_path)
    if not src.is_file():
        raise HTTPException(404, f"source not found: {src}")

    stem = _sanitize(body.name) if body.name else f"{_sanitize(src.stem)}_sfx"
    out_dir = settings.workspace_dir / "sfx_overlays"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{stem}.mp4"

    ok, err = await asyncio.to_thread(
        apply_sfx,
        str(src),
        str(out_path),
        sfx=[s.model_dump() for s in body.sfx],
    )
    if not ok:
        raise HTTPException(500, f"sfx overlay failed: {(err or '')[:500]}")
    return {"ok": True, "output_path": str(out_path)}
