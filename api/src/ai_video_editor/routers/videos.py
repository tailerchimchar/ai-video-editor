"""Videos hub API: `GET /api/v1/videos` lists every narratable video.

Loops the narration target registry; no kind is named here.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter

from ..narration import catalog
from ..narration.catalog import VideoSummary

router = APIRouter(tags=["videos"], prefix="/videos")


@router.get("", response_model=list[VideoSummary])
async def list_videos() -> list[VideoSummary]:
    # ffprobe + disk reads: keep them off the event loop.
    return await asyncio.to_thread(catalog.list_videos)
