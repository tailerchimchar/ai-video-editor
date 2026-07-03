"""Single-shot VLM validation calls.

Two entry points, both `@observe`-decorated to appear in the Langfuse
trace tree:

- `validate_clip` — one call, one verdict on a short cut clip
- `validate_compilation` — one call, one review of a rendered comp

The retry loop (per-clip iterations, whole-comp passes) lives in
`loops.py`; this module is just the "call the model, get the verdict"
layer. Both calls fall back to a `skip_vlm_unavailable` synthetic
verdict when the backend is unreachable — the compile never crashes.

Langfuse instrumentation mirrors `ranker.py:25-36,112,163-183` — the
sole in-repo template. Optional import shim keeps this module runnable
without Langfuse configured.
"""

from __future__ import annotations

import contextlib
import logging
import tempfile
from pathlib import Path
from typing import cast

from .backends.base import VLMBackend, VLMUnavailableError, run_with_retry
from .client import select_backend
from .frames import extract_frames
from .prompts import (
    ClipVerdict,
    CompilationReview,
    build_clip_system_prompt,
    build_clip_user_prompt,
    build_comp_system_prompt,
    build_comp_user_prompt,
)

try:
    from langfuse import get_client, observe
except Exception:  # langfuse optional — mirrors ranker.py:25-36

    def observe(*_a, **_k):
        def deco(fn):
            return fn

        return deco

    def get_client():  # type: ignore[misc]
        return None


_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Sentinel verdicts for graceful backend-unavailable
# ---------------------------------------------------------------------


def _skip_verdict(why: str) -> ClipVerdict:
    """Synthetic verdict used when the backend is unreachable.

    Uses `pass` so the pipeline treats the clip as valid — VLM is a
    filter, not a gate, so a broken filter should NOT drop clips.
    The `why` string records the reason for the trace.
    """
    return ClipVerdict(verdict="pass", why=f"skip_vlm_unavailable: {why}")


def _skip_review(why: str) -> CompilationReview:
    return CompilationReview(is_cohesive=True, fixes=[])


# ---------------------------------------------------------------------
# Per-clip validation
# ---------------------------------------------------------------------


@observe(name="vlm-validate-clip", as_type="generation")
def validate_clip(
    clip_path: str | Path,
    *,
    game: str | None,
    event_type: str | None,
    source: str | None,
    anchor_seconds: float | None,
    clip_duration: float,
    n_frames: int,
    backend: VLMBackend | None = None,
) -> ClipVerdict:
    """Ask the VLM to verdict-check one cut clip. Never raises."""
    backend = backend or select_backend()
    system_prompt, resolved_hints = build_clip_system_prompt(game)
    user_prompt = build_clip_user_prompt(
        event_type=event_type,
        source=source,
        anchor_seconds=anchor_seconds,
        clip_duration=clip_duration,
    )

    with tempfile.TemporaryDirectory(prefix="vlm_frames_") as tmp:
        frames = extract_frames(
            str(clip_path),
            Path(tmp),
            n_samples=n_frames,
            duration_seconds=clip_duration,
        )
        if not frames:
            verdict = _skip_verdict(f"no frames sampled from {Path(clip_path).name}")
            _annotate_trace(verdict, backend, resolved_hints, n_frames=0)
            return verdict
        try:
            verdict = cast(
                ClipVerdict,
                run_with_retry(
                    backend,
                    frames=frames,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    response_model=ClipVerdict,
                ),
            )
        except VLMUnavailableError as exc:
            verdict = _skip_verdict(str(exc))
        except Exception as exc:  # parse/validation exhaustion
            _log.warning("validate_clip parse failure after retries: %s", exc)
            verdict = _skip_verdict(f"parse_failed: {exc!s}")

    _annotate_trace(verdict, backend, resolved_hints, n_frames=len(frames))
    return verdict


# ---------------------------------------------------------------------
# Whole-compilation review
# ---------------------------------------------------------------------


@observe(name="vlm-validate-compilation", as_type="generation")
def validate_compilation(
    compilation_path: str | Path,
    *,
    game: str | None,
    clip_count: int,
    total_duration_seconds: float,
    n_frames: int,
    backend: VLMBackend | None = None,
) -> CompilationReview:
    """Ask the VLM to review a rendered compilation. Never raises."""
    backend = backend or select_backend()
    system_prompt, resolved_hints = build_comp_system_prompt(game)
    user_prompt = build_comp_user_prompt(
        clip_count=clip_count,
        total_seconds=total_duration_seconds,
    )

    with tempfile.TemporaryDirectory(prefix="vlm_frames_") as tmp:
        frames = extract_frames(
            str(compilation_path),
            Path(tmp),
            n_samples=n_frames,
            duration_seconds=total_duration_seconds,
        )
        if not frames:
            review = _skip_review("no frames sampled")
            _annotate_trace(review, backend, resolved_hints, n_frames=0)
            return review
        try:
            review = cast(
                CompilationReview,
                run_with_retry(
                    backend,
                    frames=frames,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    response_model=CompilationReview,
                ),
            )
        except VLMUnavailableError as exc:
            review = _skip_review(str(exc))
        except Exception as exc:
            _log.warning("validate_compilation parse failure after retries: %s", exc)
            review = _skip_review(f"parse_failed: {exc!s}")

    _annotate_trace(review, backend, resolved_hints, n_frames=len(frames))
    return review


# ---------------------------------------------------------------------
# Trace annotation (mirrors ranker.py:163-183)
# ---------------------------------------------------------------------


def _annotate_trace(
    result: ClipVerdict | CompilationReview,
    backend: VLMBackend,
    resolved_hints: str,
    *,
    n_frames: int,
) -> None:
    """Attach model + backend + input/output metadata to the current span.

    Best-effort — swallowed if Langfuse isn't configured (matches the
    ranker's `contextlib.suppress` pattern)."""
    lf = get_client()
    if lf is None:
        return
    with contextlib.suppress(Exception):
        model = getattr(backend, "active_model", None) or backend.name
        lf.update_current_generation(
            model=model,
            metadata={
                "backend": backend.name,
                "hints_file": resolved_hints,
                "frames": n_frames,
            },
        )
        if isinstance(result, ClipVerdict):
            lf.update_current_trace(
                output={
                    "verdict": result.verdict,
                    "fix": result.fix,
                    "fix_seconds": result.fix_seconds,
                },
                tags=["vlm", backend.name, "per-clip"],
            )
        else:
            lf.update_current_trace(
                output={
                    "is_cohesive": result.is_cohesive,
                    "num_fixes": len(result.fixes),
                },
                tags=["vlm", backend.name, "whole-comp"],
            )
