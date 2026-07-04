"""Shorts pipeline HTTP endpoints.

Three routes:

- `POST /assets/{asset_id}/shorts` — trigger a compile_shorts job. Runs
  the render pipeline in a background task tracked via the jobs table
  (same pattern as split/ingest_url). Returns a `job_id`.
- `GET /assets/{asset_id}/shorts` — list rendered shorts on disk for
  an asset in either mode. Reads `index.json`/`index.md`; never
  triggers a render.
- `GET /assets/{asset_id}/shorts/topics` — preview the bucket
  distribution (topic buckets + clip counts) without rendering
  anything.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..database import get_db
from ..models import RankResponse
from ..shorts import _shorts_folder, build_shorts, preview_buckets

router = APIRouter(tags=["shorts"])

_background_tasks: set[asyncio.Task] = set()


def _track(coro) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def _finish_job(
    db, job_id: str, *, output_path: str | None = None, error: str | None = None
) -> None:
    status = "failed" if error else "completed"
    with contextlib.suppress(Exception):
        await db.execute(
            "UPDATE jobs SET status = ?, output_path = ?, error = ?, completed_at = ? "
            "WHERE id = ?",
            (status, output_path, error, datetime.now(timezone.utc).isoformat(), job_id),
        )
        await db.commit()


class CompileShortsBody(BaseModel):
    """Payload for `POST /assets/{id}/shorts`."""

    mode: str = Field(..., description="voiceover | montage")
    topic: str | None = Field(
        default=None,
        description=(
            "Optional bucket-substring filter, case-insensitive. "
            "e.g. 'laning', 'teamfight', 'objective'. Omit for all buckets."
        ),
    )
    music_path: str | None = Field(
        default=None,
        description=(
            "Override the config default music path for montage mode. "
            "Ignored in voiceover mode."
        ),
    )


async def _load_candidates(db, asset_id: str) -> list[dict]:
    """Best-effort candidate load; the highlights folder resolver needs
    the candidate rows to derive the folder path (Riot correlation,
    champion, etc.)."""
    rows = await db.execute_fetchall(
        "SELECT * FROM highlight_candidates WHERE video_id = ?", (asset_id,)
    )
    return [dict(r) for r in rows]


async def _run_shorts_job(
    job_id: str,
    asset: dict,
    body: CompileShortsBody,
) -> None:
    """Background worker — the actual render happens here."""
    db = await get_db()
    try:
        await db.execute(
            "UPDATE jobs SET status = 'running' WHERE id = ?", (job_id,)
        )
        await db.commit()

        candidates = await _load_candidates(db, asset["id"])
        result = await asyncio.to_thread(
            build_shorts,
            asset,
            candidates,
            mode=body.mode,
            topic=body.topic,
            music_path=body.music_path,
        )
        if not result.get("ok"):
            await _finish_job(
                db, job_id, error=str(result.get("error") or "shorts render failed")
            )
            return
        await _finish_job(
            db,
            job_id,
            output_path=(
                f"{result.get('shorts_written', 0)}/"
                f"{result.get('shorts_total', 0)} -> {result.get('folder', '?')}"
            ),
        )
    except Exception as exc:
        with contextlib.suppress(Exception):
            await _finish_job(db, job_id, error=str(exc)[:1500])
    finally:
        await db.close()


@router.post("/assets/{asset_id}/shorts", response_model=RankResponse)
async def compile_shorts(asset_id: str, body: CompileShortsBody):
    """Trigger a shorts render job for the given asset.

    Requires the asset to have been analyzed with `cut=True` first (so
    the highlights folder exists). Returns a `job_id`; poll
    `/api/v1/jobs/{id}` for status.
    """
    if body.mode not in ("voiceover", "montage"):
        raise HTTPException(400, "mode must be 'voiceover' or 'montage'")

    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT * FROM assets WHERE id = ?", (asset_id,)
        )
        if not rows:
            raise HTTPException(404, "Asset not found")
        asset = dict(rows[0])
        job_id = str(uuid.uuid4())
        await db.execute(
            "INSERT INTO jobs (id, project_id, type, status, created_at) "
            "VALUES (?, NULL, 'shorts', 'pending', ?)",
            (job_id, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()
    finally:
        await db.close()

    _track(_run_shorts_job(job_id, asset, body))
    return RankResponse(job_id=job_id)


@router.get("/assets/{asset_id}/shorts")
async def list_shorts(asset_id: str, mode: str | None = None):
    """List rendered shorts on disk for this asset.

    `mode` filters to voiceover / montage subfolder; omit for both.
    Read-only — never triggers a render.
    """
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT * FROM assets WHERE id = ?", (asset_id,)
        )
        if not rows:
            raise HTTPException(404, "Asset not found")
        asset = dict(rows[0])
    finally:
        await db.close()

    modes = [mode] if mode in ("voiceover", "montage") else ["voiceover", "montage"]
    out: dict[str, dict] = {}
    for m in modes:
        folder = _shorts_folder(asset, m)
        if not folder.is_dir():
            out[m] = {"folder": None, "shorts": []}
            continue
        shorts = sorted(p.name for p in folder.glob("*.mp4"))
        index_md = folder / "index.md"
        out[m] = {
            "folder": folder.as_posix(),
            "shorts": shorts,
            "index_md": index_md.read_text(encoding="utf-8") if index_md.is_file() else None,
        }
    return out


@router.get("/assets/{asset_id}/shorts/topics")
async def preview_shorts_topics(asset_id: str):
    """Preview the bucket distribution for this asset without rendering.

    Reads the highlights folder + `index.json`, runs `categorize_clips`,
    returns per-bucket clip counts + a small clip preview. Useful before
    committing to a compile.
    """
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT * FROM assets WHERE id = ?", (asset_id,)
        )
        if not rows:
            raise HTTPException(404, "Asset not found")
        asset = dict(rows[0])
        candidates = await _load_candidates(db, asset_id)
    finally:
        await db.close()
    # `preview_buckets` is a pure read — no ffmpeg, no I/O beyond
    # `index.json`. Safe to run inline.
    _ = Path  # silence unused-import warnings under some ruff configs
    return preview_buckets(asset, candidates)
