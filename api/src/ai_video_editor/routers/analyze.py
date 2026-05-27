"""Phase 2 analyzer router.

Flow (analyzer-only — no clips are cut):
  1. POST /assets/{id}/candidates  -> background job: generate HighlightCandidates
  2. GET  /assets/{id}/candidates  -> inspect raw candidates
  3. POST /assets/{id}/rank        -> background job: LLM ranks candidates
  4. GET  /assets/{id}/rankings    -> ranked suggestions (scores + reasons)

Jobs reuse the existing jobs table + GET /jobs/{id} polling UX.
"""

import asyncio
import contextlib
import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from ..candidates.probe import get_duration_seconds
from ..candidates.service import compute_candidates
from ..candidates.transcribe import transcribe
from ..config import settings
from ..database import get_db
from ..highlights import build_clip_batch, build_highlights, relative_folder
from ..models import HighlightCandidate, RankResponse
from ..rag import index_asset_transcript
from ..rag import search as rag_search
from ..ranker import rank_candidates

router = APIRouter(tags=["analyze"])

_background_tasks: set[asyncio.Task] = set()


def _track(coro) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def _load_asset(db, asset_id: str) -> dict:
    rows = await db.execute_fetchall("SELECT * FROM assets WHERE id = ?", (asset_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="Asset not found")
    return dict(rows[0])


async def _finish(db, job_id: str, *, output_path: str | None, error: str | None) -> None:
    # Best-effort: if the DB write itself fails (e.g. disk full), don't
    # turn the error path into an unhandled task crash.
    with contextlib.suppress(Exception):
        status = "failed" if error else "completed"
        await db.execute(
            "UPDATE jobs SET status = ?, output_path = ?, error = ?, completed_at = ? WHERE id = ?",
            (status, output_path, error, datetime.now(timezone.utc).isoformat(), job_id),
        )
        await db.commit()


# --- Candidate generation ---
async def _run_candidates_job(job_id: str, asset: dict) -> None:
    db = await get_db()
    try:
        await db.execute("UPDATE jobs SET status = 'running' WHERE id = ?", (job_id,))
        await db.commit()

        # Reuse a stored transcript if one exists (transcription is a
        # separate, heavy, explicit job — never run inline here).
        # Pulling sentiment_score so the G1b high-sentiment branch
        # in detect_transcript_keywords can fire. Legacy rows (NULL
        # sentiment_score) get scored on the fly — VADER is microseconds
        # per call, so this is cheaper than a separate backfill job.
        trows = await db.execute_fetchall(
            "SELECT start_seconds, end_seconds, text, sentiment_score "
            "FROM transcripts WHERE video_id = ? ORDER BY start_seconds",
            (asset["id"],),
        )
        from ..candidates.sentiment import score_sentiment

        segments = []
        for r in trows:
            d = dict(r)
            if d.get("sentiment_score") is None and d.get("text"):
                d["sentiment_score"] = score_sentiment(d["text"])
            segments.append(d)
        rows, diag = await asyncio.to_thread(compute_candidates, asset, segments)

        # Idempotent regen: clear prior candidates for this video.
        await db.execute("DELETE FROM highlight_candidates WHERE video_id = ?", (asset["id"],))
        for r in rows:
            await db.execute(
                "INSERT INTO highlight_candidates "
                "(id, video_id, source, start_seconds, end_seconds, event_type, "
                " confidence, metadata, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    r["id"],
                    r["video_id"],
                    r["source"],
                    r["start_seconds"],
                    r["end_seconds"],
                    r["event_type"],
                    r["confidence"],
                    json.dumps(r["metadata"]),
                    r["created_at"],
                ),
            )
        await db.commit()
        summary = f"{len(rows)} candidates"
        riot = diag.get("riot")
        # Surface a degraded Riot result so it's not mistaken for "no
        # data" — rate_limited/api_error are transient & retryable.
        if riot in ("rate_limited", "api_error"):
            summary += f" (⚠️ riot {riot} — retry for kill data)"
        elif riot == "no_match":
            summary += " (riot: no confident match)"
        await _finish(db, job_id, output_path=summary, error=None)
    except Exception as e:
        await _finish(db, job_id, output_path=None, error=str(e)[:2000])
    finally:
        await db.close()


@router.post("/assets/{asset_id}/candidates", response_model=RankResponse)
async def generate_candidates(asset_id: str):
    db = await get_db()
    try:
        asset = await _load_asset(db, asset_id)
        job_id = str(uuid.uuid4())
        await db.execute(
            "INSERT INTO jobs (id, project_id, type, status, created_at) "
            "VALUES (?, NULL, 'candidates', 'pending', ?)",
            (job_id, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()
    finally:
        await db.close()

    _track(_run_candidates_job(job_id, asset))
    return RankResponse(job_id=job_id)


@router.get("/assets/{asset_id}/candidates", response_model=list[HighlightCandidate])
async def list_candidates(asset_id: str):
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT * FROM highlight_candidates WHERE video_id = ? ORDER BY start_seconds",
            (asset_id,),
        )
        out = []
        for row in rows:
            d = dict(row)
            d["metadata"] = json.loads(d["metadata"]) if d.get("metadata") else None
            out.append(HighlightCandidate(**d))
        return out
    finally:
        await db.close()


# --- LLM ranking ---
async def _run_rank_job(job_id: str, asset: dict) -> None:
    db = await get_db()
    try:
        await db.execute("UPDATE jobs SET status = 'running' WHERE id = ?", (job_id,))
        await db.commit()

        rows = await db.execute_fetchall(
            "SELECT * FROM highlight_candidates WHERE video_id = ? ORDER BY start_seconds",
            (asset["id"],),
        )
        if not rows:
            await _finish(
                db,
                job_id,
                output_path=None,
                error="No candidates — run POST /assets/{id}/candidates first",
            )
            return

        candidates = []
        for row in rows:
            d = dict(row)
            d["metadata"] = json.loads(d["metadata"]) if d.get("metadata") else None
            candidates.append(d)

        ranked = await asyncio.to_thread(rank_candidates, asset.get("game"), candidates)

        rankings_dir = settings.workspace_dir / "rankings"
        rankings_dir.mkdir(parents=True, exist_ok=True)
        out_path = (rankings_dir / f"{asset['id']}.json").as_posix()
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump([r.model_dump() for r in ranked], f, indent=2)

        await _finish(db, job_id, output_path=out_path, error=None)
    except Exception as e:
        await _finish(db, job_id, output_path=None, error=str(e)[:2000])
    finally:
        await db.close()


@router.post("/assets/{asset_id}/rank", response_model=RankResponse)
async def rank_asset(asset_id: str):
    db = await get_db()
    try:
        asset = await _load_asset(db, asset_id)
        job_id = str(uuid.uuid4())
        await db.execute(
            "INSERT INTO jobs (id, project_id, type, status, created_at) "
            "VALUES (?, NULL, 'rank', 'pending', ?)",
            (job_id, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()
    finally:
        await db.close()

    _track(_run_rank_job(job_id, asset))
    return RankResponse(job_id=job_id)


@router.get("/assets/{asset_id}/rankings")
async def get_rankings(asset_id: str):
    path = settings.workspace_dir / "rankings" / f"{asset_id}.json"
    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail="No rankings yet — run POST /assets/{id}/rank and wait for the job",
        )
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# --- Highlights: cut kept suggestions into an organized folder ---
async def _load_candidates(db, video_id: str) -> list[dict]:
    rows = await db.execute_fetchall(
        "SELECT * FROM highlight_candidates WHERE video_id = ? ORDER BY start_seconds",
        (video_id,),
    )
    out = []
    for row in rows:
        d = dict(row)
        d["metadata"] = json.loads(d["metadata"]) if d.get("metadata") else None
        out.append(d)
    return out


async def _run_highlights_job(job_id: str, asset: dict) -> None:
    db = await get_db()
    try:
        await db.execute("UPDATE jobs SET status = 'running' WHERE id = ?", (job_id,))
        await db.commit()

        rankings_path = settings.workspace_dir / "rankings" / f"{asset['id']}.json"
        if not rankings_path.exists():
            await _finish(
                db,
                job_id,
                output_path=None,
                error="No rankings — run POST /assets/{id}/rank first",
            )
            return
        rankings = json.loads(rankings_path.read_text(encoding="utf-8"))
        candidates = await _load_candidates(db, asset["id"])

        summary = await asyncio.to_thread(build_highlights, asset, rankings, candidates)
        msg = f"{summary['clips_written']}/{summary['clips_total']} -> {summary['folder']}"
        await _finish(db, job_id, output_path=msg, error=None)
    except Exception as e:
        await _finish(db, job_id, output_path=None, error=str(e)[:2000])
    finally:
        await db.close()


@router.post("/assets/{asset_id}/highlights", response_model=RankResponse)
async def cut_highlights(asset_id: str):
    db = await get_db()
    try:
        asset = await _load_asset(db, asset_id)
        job_id = str(uuid.uuid4())
        await db.execute(
            "INSERT INTO jobs (id, project_id, type, status, created_at) "
            "VALUES (?, NULL, 'highlights', 'pending', ?)",
            (job_id, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()
    finally:
        await db.close()

    _track(_run_highlights_job(job_id, asset))
    return RankResponse(job_id=job_id)


@router.get("/assets/{asset_id}/highlights")
async def get_highlights(asset_id: str):
    db = await get_db()
    try:
        asset = await _load_asset(db, asset_id)
        candidates = await _load_candidates(db, asset_id)
    finally:
        await db.close()

    index = settings.workspace_dir / relative_folder(asset, candidates) / "index.json"
    if not index.exists():
        raise HTTPException(
            status_code=404,
            detail="No highlights yet — run POST /assets/{id}/highlights and wait for the job",
        )
    return json.loads(index.read_text(encoding="utf-8"))


# --- Batch: pool many short Outplayed clips for a game into one folder ---
async def _run_clip_batch_job(job_id: str, game: str, limit: int) -> None:
    db = await get_db()
    try:
        await db.execute("UPDATE jobs SET status = 'running' WHERE id = ?", (job_id,))
        await db.commit()

        rows = await db.execute_fetchall("SELECT * FROM assets")
        g = game.lower()
        assets = [
            dict(r)
            for r in rows
            if g in f"{dict(r).get('game') or ''} {dict(r)['filename']}".lower()
        ]
        # Short files == Outplayed event clips. Newest first, capped.
        clips = [
            a
            for a in assets
            if 0 < get_duration_seconds(a["path"]) <= settings.outplayed_clip_max_seconds
        ]
        clips.sort(key=lambda a: a["created_at"], reverse=True)
        clips = clips[:limit]
        if not clips:
            await _finish(
                db, job_id, output_path=None, error=f"No short Outplayed clips for {game!r}"
            )
            return

        # Organize-only: Outplayed already curated these; no LLM ($0).
        summary = await asyncio.to_thread(build_clip_batch, game, clips)
        msg = f"{summary['clips_written']}/{summary['clips_total']} -> {summary['folder']}"
        await _finish(db, job_id, output_path=msg, error=None)
    except Exception as e:
        await _finish(db, job_id, output_path=None, error=str(e)[:2000])
    finally:
        await db.close()


@router.post("/clips/batch-highlights", response_model=RankResponse)
async def batch_clip_highlights(game: str, limit: int = 30):
    """Pool a game's short Outplayed clips, rank them in one LLM call, and
    cut the kept ones into highlights/<game>/clips_<date>/."""
    db = await get_db()
    try:
        job_id = str(uuid.uuid4())
        await db.execute(
            "INSERT INTO jobs (id, project_id, type, status, created_at) "
            "VALUES (?, NULL, 'batch_highlights', 'pending', ?)",
            (job_id, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()
    finally:
        await db.close()

    _track(_run_clip_batch_job(job_id, game, limit))
    return RankResponse(job_id=job_id)


# --- Transcription (local Whisper — heavy, so its own explicit job) ---
async def _run_transcribe_job(job_id: str, asset: dict) -> None:
    db = await get_db()
    try:
        await db.execute("UPDATE jobs SET status = 'running' WHERE id = ?", (job_id,))
        await db.commit()

        duration = get_duration_seconds(asset["path"])
        segments = await asyncio.to_thread(transcribe, asset["path"], duration, asset.get("game"))

        # Idempotent regen.
        await db.execute("DELETE FROM transcripts WHERE video_id = ?", (asset["id"],))
        now = datetime.now(timezone.utc).isoformat()
        for s in segments:
            await db.execute(
                "INSERT INTO transcripts "
                "(id, video_id, start_seconds, end_seconds, text, sentiment_score, "
                " words, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(uuid.uuid4()),
                    asset["id"],
                    s["start_seconds"],
                    s["end_seconds"],
                    s["text"],
                    s.get("sentiment_score"),
                    json.dumps(s.get("words")) if s.get("words") else None,
                    now,
                ),
            )
        await db.commit()
        msg = f"{len(segments)} transcript segments"
        if not segments:
            msg += " (empty — long/over-cap, no speech, or whisper unavailable)"
        await _finish(db, job_id, output_path=msg, error=None)
    except Exception as e:
        await _finish(db, job_id, output_path=None, error=str(e)[:2000])
    finally:
        await db.close()


@router.post("/assets/{asset_id}/transcribe", response_model=RankResponse)
async def transcribe_asset(asset_id: str):
    db = await get_db()
    try:
        asset = await _load_asset(db, asset_id)
        job_id = str(uuid.uuid4())
        await db.execute(
            "INSERT INTO jobs (id, project_id, type, status, created_at) "
            "VALUES (?, NULL, 'transcribe', 'pending', ?)",
            (job_id, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()
    finally:
        await db.close()

    _track(_run_transcribe_job(job_id, asset))
    return RankResponse(job_id=job_id)


@router.get("/assets/{asset_id}/transcript")
async def get_transcript(asset_id: str):
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT start_seconds, end_seconds, text, words FROM transcripts "
            "WHERE video_id = ? ORDER BY start_seconds",
            (asset_id,),
        )
    finally:
        await db.close()
    # `words` is JSON text in the DB (one row per Whisper segment, written
    # by the transcribe job). Hydrate to a list so consumers don't need
    # to re-parse — matches the compile path's shape.
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        d["words"] = json.loads(d["words"]) if d.get("words") else []
        out.append(d)
    return out


# --- RAG: index transcript → vectors; semantic search ---
async def _run_index_job(job_id: str, asset_id: str) -> None:
    db = await get_db()
    try:
        await db.execute("UPDATE jobs SET status = 'running' WHERE id = ?", (job_id,))
        await db.commit()
        summary = await index_asset_transcript(db, asset_id)
        msg = (
            f"{summary['indexed']}/{summary['chunks']} chunks indexed "
            f"({summary['segments']} transcript segments)"
        )
        await _finish(db, job_id, output_path=msg, error=None)
    except Exception as e:
        await _finish(db, job_id, output_path=None, error=str(e)[:2000])
    finally:
        await db.close()


@router.post("/assets/{asset_id}/index", response_model=RankResponse)
async def index_asset(asset_id: str):
    """Embed this asset's transcript chunks into the vector store
    (sqlite-vec). Requires the asset to have been transcribed first."""
    db = await get_db()
    try:
        await _load_asset(db, asset_id)
        job_id = str(uuid.uuid4())
        await db.execute(
            "INSERT INTO jobs (id, project_id, type, status, created_at) "
            "VALUES (?, NULL, 'index', 'pending', ?)",
            (job_id, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()
    finally:
        await db.close()

    _track(_run_index_job(job_id, asset_id))
    return RankResponse(job_id=job_id)


@router.get("/search")
async def search(q: str, limit: int = 10, asset_id: str | None = None):
    """Semantic search over indexed transcript chunks."""
    db = await get_db()
    try:
        return await rag_search(db, q, limit=limit, asset_id=asset_id)
    finally:
        await db.close()
