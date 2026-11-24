"""Hand-picked cloud copies of raw VODs (Supabase Storage).

Upload is a background job (multi-GB files); list and remove are immediate.
Only the cloud copy is ever removed — the local recording is never touched.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import storage
from ..candidates.probe import get_duration_seconds
from ..config import settings
from ..database import get_db
from ..jobs import finish_job
from ..models import RankResponse
from ..thumbnail import safe_extract_asset_thumbnail

router = APIRouter(tags=["cloud"])

_background_tasks: set[asyncio.Task] = set()


def _track(coro) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def _require_configured() -> None:
    if not storage.is_configured():
        raise HTTPException(503, "Cloud storage not configured (SUPABASE_S3_* in .env)")


async def _asset_and_key(asset_id: str) -> tuple[dict, str]:
    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT * FROM assets WHERE id = ?", (asset_id,))
    finally:
        await db.close()
    if not rows:
        raise HTTPException(404, "Asset not found")
    asset = dict(rows[0])
    try:
        key = storage.vod_key(Path(asset["path"]))
    except ValueError as e:
        raise HTTPException(400, "Asset is not under OUTPLAYED_MEDIA_DIR") from e
    return asset, key


def _upload_sync(path: Path, key: str) -> dict:
    s3 = storage.client()
    bucket = settings.storage_outplayed_bucket
    size = path.stat().st_size
    existing = storage.remote_size(s3, bucket, key)
    if existing == size:
        return {"key": key, "bytes": size, "already_uploaded": True}
    after = storage.projected_total(storage.stored_bytes(s3), existing or 0, size)
    if storage.over_budget(after, settings.storage_max_total_gb):
        raise RuntimeError(
            f"would exceed the {settings.storage_max_total_gb:g} GB cloud budget "
            f"({after / 1024**3:.1f} GB after upload); remove something first"
        )
    storage.upload_file(s3, bucket, key, path)
    return {"key": key, "bytes": size, "already_uploaded": False, "total_bytes_after": after}


async def _run_upload_job(job_id: str, path: Path, key: str) -> None:
    db = await get_db()
    try:
        await db.execute("UPDATE jobs SET status = 'running' WHERE id = ?", (job_id,))
        await db.commit()
        result = await asyncio.to_thread(_upload_sync, path, key)
        note = " (already uploaded)" if result["already_uploaded"] else ""
        await finish_job(
            db,
            job_id,
            output_path=f"{settings.storage_outplayed_bucket}:{key}{note}",
            job_type="cloud_upload",
        )
    except Exception as exc:
        with contextlib.suppress(Exception):
            await finish_job(db, job_id, error=str(exc)[:1500], job_type="cloud_upload")
    finally:
        await db.close()


@router.post("/assets/{asset_id}/cloud", response_model=RankResponse)
async def upload_vod(asset_id: str) -> RankResponse:
    """Copy one recording into the `outplayed` bucket at its relative path. Job-based."""
    _require_configured()
    asset, key = await _asset_and_key(asset_id)
    path = Path(asset["path"])
    if asset.get("source_deleted_at") or not path.is_file():
        raise HTTPException(400, "Source file is not on disk")

    job_id = str(uuid.uuid4())
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO jobs (id, project_id, type, status, created_at) "
            "VALUES (?, NULL, 'cloud_upload', 'pending', ?)",
            (job_id, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()
    finally:
        await db.close()
    _track(_run_upload_job(job_id, path, key))
    return RankResponse(job_id=job_id)


@router.get("/cloud/vods")
async def list_cloud_vods() -> dict:
    """VODs in the cloud, matched back to local assets, plus budget usage."""
    _require_configured()

    def _list() -> tuple[list[dict], int]:
        s3 = storage.client()
        return storage.list_objects(s3, settings.storage_outplayed_bucket), storage.stored_bytes(s3)

    objects, used = await asyncio.to_thread(_list)

    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT id, path FROM assets")
    finally:
        await db.close()
    by_key: dict[str, str] = {}
    for r in rows:
        with contextlib.suppress(ValueError):
            by_key[storage.vod_key(Path(r["path"]))] = r["id"]

    return {
        "vods": [{**o, "asset_id": by_key.get(o["key"])} for o in objects],
        "used_bytes": used,
        "budget_gb": settings.storage_max_total_gb,
    }


class PullBody(BaseModel):
    key: str = Field(min_length=1, max_length=1024)


def _download_sync(key: str, dest: Path) -> dict:
    s3 = storage.client()
    bucket = settings.storage_outplayed_bucket
    size = storage.remote_size(s3, bucket, key)
    if size is None:
        raise RuntimeError(f"not in the cloud: {key}")
    if dest.is_file() and dest.stat().st_size == size:
        return {"downloaded": False, "bytes": size}
    storage.download_file(s3, bucket, key, dest)
    return {"downloaded": True, "bytes": size}


async def _register_pulled(db, dest: Path, downloaded: bool) -> str:
    """Index the pulled file. Only a file this pull created is marked
    `source_origin='cloud'` (deletable); a recording that was already on disk
    keeps its origin and is never made deletable by a pull."""
    path = str(dest)
    rows = await db.execute_fetchall("SELECT id, source_origin FROM assets WHERE path = ?", (path,))
    if rows:
        asset_id, origin = rows[0]["id"], rows[0]["source_origin"]
        if downloaded or origin == "cloud":
            await db.execute(
                "UPDATE assets SET source_deleted_at = NULL, source_origin = 'cloud' WHERE id = ?",
                (asset_id,),
            )
        return asset_id

    root = settings.outplayed_media_dir.resolve()
    game = dest.parent.name if dest.parent != root else None
    try:
        dur = get_duration_seconds(path)
    except Exception:
        dur = None
    now = datetime.now(timezone.utc).isoformat()
    asset_id = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO assets (id, filename, path, game, created_at, indexed_at, "
        "duration_seconds, source_origin) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            asset_id,
            dest.name,
            path,
            game,
            now,
            now,
            dur if dur and dur > 0 else None,
            "cloud" if downloaded else "imported",
        ),
    )
    safe_extract_asset_thumbnail(asset_id, path)
    return asset_id


async def _run_pull_job(job_id: str, key: str, dest: Path) -> None:
    db = await get_db()
    try:
        await db.execute("UPDATE jobs SET status = 'running' WHERE id = ?", (job_id,))
        await db.commit()
        result = await asyncio.to_thread(_download_sync, key, dest)
        asset_id = await _register_pulled(db, dest, result["downloaded"])
        await db.commit()
        await finish_job(db, job_id, output_path=asset_id, job_type="cloud_pull")
    except Exception as exc:
        with contextlib.suppress(Exception):
            await finish_job(db, job_id, error=str(exc)[:1500], job_type="cloud_pull")
    finally:
        await db.close()


@router.post("/cloud/vods/pull", response_model=RankResponse)
async def pull_vod(body: PullBody) -> RankResponse:
    """Download a cloud VOD to the same relative path under OUTPLAYED_MEDIA_DIR
    and index it. Job-based; `output_path` is the asset id on success."""
    _require_configured()
    try:
        dest = storage.local_path_for_key(body.key)
    except ValueError as e:
        raise HTTPException(400, "Invalid key") from e

    job_id = str(uuid.uuid4())
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO jobs (id, project_id, type, status, created_at) "
            "VALUES (?, NULL, 'cloud_pull', 'pending', ?)",
            (job_id, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()
    finally:
        await db.close()
    _track(_run_pull_job(job_id, body.key, dest))
    return RankResponse(job_id=job_id)


def cloud_copy_matches(path: Path) -> bool:
    """True when the cloud holds this recording at the same size (safe to drop locally)."""
    if not storage.is_configured() or not path.is_file():
        return False
    try:
        key = storage.vod_key(path)
        return storage.remote_size(storage.client(), settings.storage_outplayed_bucket, key) == (
            path.stat().st_size
        )
    except Exception:
        return False


@router.delete("/assets/{asset_id}/cloud")
async def remove_cloud_vod(asset_id: str) -> dict:
    """Delete the cloud copy only. The local recording is untouched."""
    _require_configured()
    _, key = await _asset_and_key(asset_id)
    bucket = settings.storage_outplayed_bucket

    def _delete() -> bool:
        s3 = storage.client()
        if storage.remote_size(s3, bucket, key) is None:
            return False
        storage.delete_object(s3, bucket, key)
        return True

    removed = await asyncio.to_thread(_delete)
    return {"asset_id": asset_id, "key": key, "removed": removed}
