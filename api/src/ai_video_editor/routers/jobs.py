from fastapi import APIRouter, HTTPException

from ..database import get_db
from ..models import JobOut

router = APIRouter(tags=["jobs"])


@router.get("/jobs/{job_id}", response_model=JobOut)
async def get_job(job_id: str):
    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT * FROM jobs WHERE id = ?", (job_id,))
        if not rows:
            raise HTTPException(status_code=404, detail="Job not found")
        return JobOut(**dict(rows[0]))
    finally:
        await db.close()
