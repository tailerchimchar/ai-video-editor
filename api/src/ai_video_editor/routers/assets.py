import os
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from ..config import settings
from ..database import get_db
from ..models import AssetOut, ScanResponse

router = APIRouter(tags=["assets"])


@router.post("/assets/scan", response_model=ScanResponse)
async def scan_assets():
    media_dir = settings.outplayed_media_dir
    if not media_dir.exists():
        raise HTTPException(status_code=400, detail=f"Media directory not found: {media_dir}")

    db = await get_db()
    try:
        new_count = 0
        for root, _dirs, files in os.walk(media_dir):
            for filename in files:
                if not filename.lower().endswith(".mp4"):
                    continue
                filepath = os.path.join(root, filename)
                # Check if already indexed
                row = await db.execute_fetchall("SELECT id FROM assets WHERE path = ?", (filepath,))
                if row:
                    continue

                # Infer game from parent folder name
                parent = os.path.basename(os.path.dirname(filepath))
                game = parent if parent != os.path.basename(str(media_dir)) else None

                # Get file creation time
                stat = os.stat(filepath)
                created_at = datetime.fromtimestamp(stat.st_ctime, tz=timezone.utc).isoformat()

                asset_id = str(uuid.uuid4())
                indexed_at = datetime.now(timezone.utc).isoformat()

                await db.execute(
                    "INSERT INTO assets (id, filename, path, game, created_at, indexed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (asset_id, filename, filepath, game, created_at, indexed_at),
                )
                new_count += 1

        await db.commit()
        total = await db.execute_fetchall("SELECT COUNT(*) as cnt FROM assets")
        return ScanResponse(new_assets=new_count, total_assets=total[0][0])
    finally:
        await db.close()


@router.get("/assets", response_model=list[AssetOut])
async def list_assets():
    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT * FROM assets ORDER BY created_at DESC")
        return [AssetOut(**dict(row)) for row in rows]
    finally:
        await db.close()
