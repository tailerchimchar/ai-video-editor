"""VLM health + review endpoints.

Two routes:

- `POST /vlm/health` — probe the configured backend, report reachable
  status + active model + canary latency. Zero side effects.
- `POST /compilations/{compilation_id}/vlm_review` — run the whole-comp
  review loop. Returns the list of suggested fixes and, in review-only
  mode, does not mutate the spec. Fix application is a follow-up MCP
  action the caller chooses to run (or not).
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..config import settings
from ..database import get_db
from ..vlm.client import UnsupportedVLMBackendError, select_backend
from ..vlm.loops import review_and_apply_compilation

router = APIRouter(tags=["vlm"])


class VLMHealthResponse(BaseModel):
    """Health snapshot — `ok=False` carries a human-readable `reason`."""

    ok: bool
    backend: str
    enabled: bool
    model: str | None = None
    latency_ms: int | None = None
    reason: str | None = None


@router.post("/vlm/health", response_model=VLMHealthResponse)
async def vlm_health() -> VLMHealthResponse:
    """Report whether the VLM backend is reachable + which model is active."""
    if not settings.vlm_enabled:
        return VLMHealthResponse(
            ok=False,
            backend=settings.vlm_backend,
            enabled=False,
            reason="VLM_ENABLED=false",
        )
    try:
        backend = select_backend()
    except UnsupportedVLMBackendError as exc:
        return VLMHealthResponse(
            ok=False,
            backend=settings.vlm_backend,
            enabled=True,
            reason=str(exc),
        )
    # Backend calls a subprocess-ish HTTP client — offload from the
    # event loop so we don't block other requests.
    result = await asyncio.to_thread(backend.health)
    return VLMHealthResponse(
        ok=bool(result.get("ok")),
        backend=backend.name,
        enabled=True,
        model=result.get("model"),
        latency_ms=result.get("latency_ms"),
        reason=result.get("reason"),
    )


class VLMReviewBody(BaseModel):
    """Optional overrides on the review pass."""

    max_passes: int | None = None
    n_frames: int | None = None


class VLMReviewFix(BaseModel):
    clip_ref: str
    issue: str
    fix: str
    fix_seconds: float | None = None
    roi: str | None = None
    focus_x: float | None = None
    focus_y: float | None = None


class VLMReviewResponse(BaseModel):
    ok: bool
    passes: int
    is_cohesive: bool
    fixes: list[VLMReviewFix]
    backend: str
    model: str | None = None


@router.post(
    "/edit/compile/{compilation_id}/vlm_review",
    response_model=VLMReviewResponse,
)
async def review_compilation(
    compilation_id: str,
    body: VLMReviewBody | None = None,
) -> VLMReviewResponse:
    """Run the whole-comp VLM review loop in review-only mode.

    Returns the list of suggested fixes. Fix application is a separate
    step (via the existing MCP editing tools) so a user can inspect the
    review before letting the loop mutate their compilation.
    """
    if not settings.vlm_enabled:
        raise HTTPException(400, "VLM_ENABLED=false")

    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT * FROM compilations WHERE id = ?", (compilation_id,)
        )
        if not rows:
            raise HTTPException(404, "Compilation not found")
        comp = dict(rows[0])
    finally:
        await db.close()

    output_path = comp.get("output_path")
    if not output_path:
        raise HTTPException(
            400,
            "Compilation has no rendered output — run POST /compile first.",
        )

    # Resolve the asset row so we know the game (for prompt hints).
    db = await get_db()
    try:
        asset_rows = await db.execute_fetchall(
            "SELECT game FROM assets WHERE id = ?", (comp.get("asset_id"),)
        )
        game = dict(asset_rows[0]).get("game") if asset_rows else None
    finally:
        await db.close()

    from pathlib import Path

    from ..compile import load_spec

    # Best-effort clip count for the prompt; falls back to 0 if the
    # spec is missing/corrupt.
    try:
        folder = Path(output_path).parent
        spec = load_spec(folder)
        clip_count = len(spec.get("clips") or [])
    except Exception:
        clip_count = 0

    max_passes = (body.max_passes if body else None) or settings.vlm_max_comp_iter
    n_frames = (body.n_frames if body else None) or settings.vlm_frame_samples_comp

    backend = select_backend()

    # review_and_apply_compilation with apply_fixes_fn=None → review-only.
    result = await asyncio.to_thread(
        review_and_apply_compilation,
        compilation_id=compilation_id,
        compilation_path=Path(output_path),
        game=(game or None),
        clip_count=clip_count,
        backend=backend,
        max_passes=max_passes,
        n_frames=n_frames,
    )

    return VLMReviewResponse(
        ok=result.ok,
        passes=result.passes,
        is_cohesive=result.final_review.is_cohesive,
        fixes=[
            VLMReviewFix(
                clip_ref=f.clip_ref,
                issue=f.issue,
                fix=f.fix,
                fix_seconds=f.fix_seconds,
                roi=f.roi,
                focus_x=f.focus_x,
                focus_y=f.focus_y,
            )
            for f in result.unapplied_fixes
        ],
        backend=backend.name,
        model=getattr(backend, "active_model", None),
    )
