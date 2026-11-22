"""Per-clip source analyzer — VLM watches ONE unfiltered clip and
returns a trim recommendation (buildup_start, payoff_anchor,
resolution_end) + payoff type + confidence.

Called before the compile so we can re-cut Outplayed / custom clips
to the recommended window using ffmpeg `-ss` / `-t`. Complements
`validator.py` — that one runs AFTER render on the compiled short.

Cache: `<clip>.analysis.json` sidecar next to the source file. Cache
key is the tuple (prompt_version, model). Bumping the rubric or the
model invalidates all sidecars.
"""

from __future__ import annotations

import contextlib
import json
import logging
import tempfile
from pathlib import Path
from typing import cast

from ..config import settings
from .backends.base import VLMBackend, VLMUnavailableError, run_with_retry
from .client import select_backend
from .corrections import effective_prompt_version
from .frames import extract_frames
from .prompts import (
    ClipAnalysis,
    build_analyzer_system_prompt,
    build_analyzer_user_prompt,
    game_hints_prompt_version,
)

try:
    from langfuse import get_client, observe
except Exception:  # langfuse optional (mirrors validator.py)

    def observe(*_a, **_k):
        def deco(fn):
            return fn

        return deco

    def get_client():  # type: ignore[misc]
        return None


_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Cache — sidecar JSON next to the clip
# ---------------------------------------------------------------------


def _cache_path(clip_path: Path) -> Path:
    return clip_path.with_suffix(clip_path.suffix + ".analysis.json")


def _resolved_model(backend: VLMBackend) -> str:
    """Best-effort model identifier — surfaces `active_model` when the
    backend exposes it (Anthropic does), else the backend name."""
    return getattr(backend, "active_model", None) or backend.name


def _load_cached(
    cache: Path, *, accepted_versions: tuple[str | int, ...], expected_model: str
) -> tuple[ClipAnalysis, str | int] | None:
    """Return (analysis, its stamped prompt_version) if the sidecar exists
    AND its (prompt_version, model) match. Otherwise None (caller re-runs).

    `accepted_versions` holds the current effective version plus the legacy
    integer marker, so sidecars written before the effective-version key
    are reused instead of re-billing the library."""
    if not cache.is_file():
        return None
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    stamped = data.get("prompt_version")
    if stamped not in accepted_versions:
        return None
    if data.get("model") != expected_model:
        return None
    payload = data.get("analysis")
    if not isinstance(payload, dict):
        return None
    try:
        return ClipAnalysis.model_validate(payload), stamped
    except Exception:  # bad shape — invalidate
        return None


def _save_cached(
    cache: Path,
    analysis: ClipAnalysis,
    *,
    prompt_version: str,
    model: str,
) -> None:
    payload = {
        "prompt_version": prompt_version,
        "model": model,
        "analysis": analysis.model_dump(),
    }
    try:
        cache.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError as exc:
        _log.info("analysis cache write failed (non-fatal): %s", exc)


# ---------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------


@observe(name="vlm-analyze-source-clip", as_type="generation")
def analyze_source_clip(
    clip_path: str | Path,
    *,
    game: str | None,
    clip_duration_seconds: float,
    n_frames: int | None = None,
    backend: VLMBackend | None = None,
    use_cache: bool | None = None,
    asset_id: str | None = None,
) -> ClipAnalysis | None:
    """Analyze one source clip. Returns None (never raises) when the
    VLM is disabled / unreachable / returns garbage — callers should
    fall back to the untrimmed clip in that case.

    - `game` selects the editing rubric under `vlm/game_hints/<game>.md`
    - Cached per-clip via a sidecar JSON. Bump `PROMPT_VERSION` in the
      hint file to invalidate.
    """
    path = Path(clip_path)
    if not path.is_file():
        return None
    if not settings.vlm_enabled or not settings.vlm_clip_analyzer_enabled:
        return None

    n = n_frames if n_frames is not None else settings.vlm_clip_analyzer_frames
    backend = backend or select_backend(asset_id=asset_id)
    model = _resolved_model(backend)
    version = effective_prompt_version(game)
    legacy_version = game_hints_prompt_version(game)

    cache_on = settings.vlm_clip_analyzer_cache_enabled if use_cache is None else use_cache
    cache = _cache_path(path)
    if cache_on:
        hit = _load_cached(
            cache,
            accepted_versions=(version, legacy_version),
            expected_model=model,
        )
        if hit is not None:
            cached, stamped = hit
            if stamped != version:
                # Legacy sidecar: migrate it to the current version (no VLM
                # call) so the next correction in the top-K invalidates it.
                _save_cached(cache, cached, prompt_version=version, model=model)
            _annotate_trace(cached, backend, game, n_frames=0, cached=True)
            return cached

    system_prompt, resolved_hints = build_analyzer_system_prompt(game)
    user_prompt = build_analyzer_user_prompt(clip_duration_seconds)

    with tempfile.TemporaryDirectory(prefix="vlm_analyze_") as tmp:
        frames = extract_frames(
            str(path),
            Path(tmp),
            n_samples=n,
            duration_seconds=clip_duration_seconds,
        )
        if not frames:
            _log.info("analyzer skipped: no frames sampled from %s", path.name)
            return None
        try:
            analysis = cast(
                ClipAnalysis,
                run_with_retry(
                    backend,
                    frames=frames,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    response_model=ClipAnalysis,
                ),
            )
        except VLMUnavailableError as exc:
            _log.info("analyzer skipped: backend unavailable: %s", exc)
            return None
        except Exception as exc:
            _log.warning("analyzer parse failure after retries: %s", exc)
            return None

    clamped = _clamp(analysis, clip_duration_seconds)
    if cache_on:
        _save_cached(cache, clamped, prompt_version=version, model=model)
    _annotate_trace(clamped, backend, game, n_frames=len(frames), cached=False)
    _ = resolved_hints  # tracked in _annotate_trace via game name
    return clamped


def _clamp_moment(
    anchor: float, start: float, end: float, duration: float
) -> tuple[float, float, float]:
    """Return (start, anchor, end) clamped to [0, duration] with the
    ordering invariant. Fills a degenerate window with a 5s bracket
    around the anchor."""
    anchor = _bound(anchor, 0.0, duration)
    start = _bound(start, 0.0, anchor)
    end = _bound(end, anchor, duration)
    if end <= start:
        half = 2.5
        start = max(0.0, anchor - half)
        end = min(duration, anchor + half)
    return round(start, 3), round(anchor, 3), round(end, 3)


def _clamp(analysis: ClipAnalysis, clip_duration: float) -> ClipAnalysis:
    """Enforce ordering + bounds without failing validation. VLMs
    sometimes return `resolution_end > duration`; clamp rather than
    reject so a mostly-good analysis still ships. Also clamps every
    entry in `additional_moments`."""
    from .prompts import PayoffMoment  # local — avoid cycle

    duration = max(clip_duration, 0.0)
    p_start, p_anchor, p_end = _clamp_moment(
        analysis.payoff_anchor_seconds,
        analysis.buildup_start_seconds,
        analysis.resolution_end_seconds,
        duration,
    )
    extras: list[PayoffMoment] = []
    for m in analysis.additional_moments:
        s, a, e = _clamp_moment(
            m.payoff_anchor_seconds,
            m.buildup_start_seconds,
            m.resolution_end_seconds,
            duration,
        )
        extras.append(
            PayoffMoment(
                payoff_anchor_seconds=a,
                buildup_start_seconds=s,
                resolution_end_seconds=e,
                payoff_type=m.payoff_type,
                description=m.description,
                confidence=m.confidence,
            )
        )

    # Drop any additional moments whose window overlaps the primary
    # (VLMs occasionally emit duplicate anchors). Rule: drop if a
    # moment's window intersects the primary's window.
    def _overlaps(m: PayoffMoment) -> bool:
        return not (m.resolution_end_seconds <= p_start or m.buildup_start_seconds >= p_end)

    extras = [m for m in extras if not _overlaps(m)]
    return ClipAnalysis(
        payoff_anchor_seconds=p_anchor,
        buildup_start_seconds=p_start,
        resolution_end_seconds=p_end,
        payoff_type=analysis.payoff_type,
        description=analysis.description,
        confidence=analysis.confidence,
        additional_moments=extras,
    )


def _bound(x: float, lo: float, hi: float) -> float:
    if hi < lo:
        hi = lo
    return max(lo, min(hi, x))


def _annotate_trace(
    analysis: ClipAnalysis,
    backend: VLMBackend,
    game: str | None,
    *,
    n_frames: int,
    cached: bool,
) -> None:
    lf = get_client()
    if lf is None:
        return
    with contextlib.suppress(Exception):
        lf.update_current_generation(
            model=_resolved_model(backend),
            metadata={
                "backend": backend.name,
                "game": game,
                "frames": n_frames,
                "cached": cached,
            },
        )
        lf.update_current_trace(
            output={
                "payoff_type": analysis.payoff_type,
                "confidence": analysis.confidence,
                "trim_window": (
                    f"{analysis.buildup_start_seconds:.2f}s..."
                    f"{analysis.resolution_end_seconds:.2f}s"
                ),
            },
            tags=["vlm", backend.name, "clip-analyzer"],
        )
