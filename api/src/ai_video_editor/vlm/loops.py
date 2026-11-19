"""VLM validation loops — the retry logic on top of validator.py.

Two loops:

- `validate_and_cut` — per-clip. Cut → validate → apply window fix →
  re-cut → validate → ... until pass / false_positive / iter cap.
  Returns the final written clip path or None (skip). Iteration cap
  configurable via `VLM_MAX_CLIP_ITER` (default 5).

- `review_and_apply_compilation` — whole-comp. Sample → review →
  apply fixes → re-render → re-sample → ... until cohesive / iter
  cap. Cap configurable via `VLM_MAX_COMP_ITER` (default 3). Fix
  application uses the existing pure spec mutators in
  `compile.py` (`extend_clip`, `remove_clip`, `add_effect`).

Both loops emit a nested `@observe` span per iteration so the Langfuse
trace tree matches the shape sketched in the plan file.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from ..candidates.probe import get_duration_seconds
from ..config import settings
from ..editing import trim_clip
from .backends.base import VLMBackend
from .client import select_backend
from .prompts import ClipVerdict, CompilationFix, CompilationReview
from .validator import validate_clip, validate_compilation

try:
    from langfuse import observe
except Exception:  # optional — mirrors ranker.py

    def observe(*_a, **_k):
        def deco(fn):
            return fn

        return deco


_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Per-clip loop
# ---------------------------------------------------------------------


@dataclass
class ClipLoopResult:
    """Outcome of the per-clip validation loop."""

    ok: bool
    """True if a clip was written; False on skip."""

    out_path: Path | None
    """Path to the written clip (None on skip)."""

    final_verdict: ClipVerdict
    """The verdict that ended the loop."""

    iterations: int
    """How many VLM calls fired (1 = pass on first try)."""

    start_seconds: float
    end_seconds: float
    """The final cut window that was written (or last attempted)."""

    verdict_history: list[ClipVerdict] = field(default_factory=list)


def _apply_window_fix(
    start: float,
    end: float,
    duration: float,
    verdict: ClipVerdict,
) -> tuple[float, float]:
    """Apply a `fixable` verdict's shift to the current window.

    Bounded by the source duration so we can't extend past t=0 or the
    end of the source. If the fix would move the boundary the wrong
    way (e.g. `trim_end` past `start`), the fix is silently clamped
    rather than raising — the loop will just re-validate the clamped
    window on the next iteration.
    """
    seconds = verdict.fix_seconds or 0.0
    if verdict.fix == "extend_before":
        return max(0.0, start - seconds), end
    if verdict.fix == "extend_after":
        return start, min(duration, end + seconds)
    if verdict.fix == "trim_start":
        return min(end - 0.5, start + seconds), end
    if verdict.fix == "trim_end":
        return start, max(start + 0.5, end - seconds)
    return start, end


@observe(name="vlm-per-clip-loop")
def validate_and_cut(
    *,
    source_path: str,
    out_path: Path,
    start: float,
    end: float,
    game: str | None,
    event_type: str | None,
    source: str | None,
    anchor_seconds: float | None,
    backend: VLMBackend | None = None,
    max_iter: int | None = None,
    n_frames: int | None = None,
) -> ClipLoopResult:
    """Cut → validate → retry loop for a single candidate.

    Never raises. On any unrecoverable error the loop degrades to a
    single cut with a `skip_vlm_unavailable`-style verdict — the clip
    is written and the compile continues.
    """
    backend = backend or select_backend()
    max_iter = max_iter or settings.vlm_max_clip_iter
    n_frames = n_frames or settings.vlm_frame_samples_clip
    duration = get_duration_seconds(source_path)

    history: list[ClipVerdict] = []
    cur_start, cur_end = start, end
    last_verdict: ClipVerdict | None = None

    for iteration in range(1, max_iter + 1):
        ok, err = trim_clip(source_path, out_path.as_posix(), cur_start, cur_end)
        if not ok:
            # Cut itself failed — no retry helps this. Log + skip.
            _log.warning("trim_clip failed at iter %d: %s", iteration, err)
            return ClipLoopResult(
                ok=False,
                out_path=None,
                final_verdict=ClipVerdict(
                    verdict="pass",  # not the VLM's fault
                    why=f"trim_failed: {err}",
                ),
                iterations=iteration,
                start_seconds=cur_start,
                end_seconds=cur_end,
                verdict_history=history,
            )

        verdict = _validate_clip_iter(
            iteration,
            clip_path=out_path,
            game=game,
            event_type=event_type,
            source=source,
            anchor_seconds=anchor_seconds,
            clip_duration=cur_end - cur_start,
            n_frames=n_frames,
            backend=backend,
        )
        history.append(verdict)
        last_verdict = verdict

        if verdict.verdict == "pass":
            return ClipLoopResult(
                ok=True,
                out_path=out_path,
                final_verdict=verdict,
                iterations=iteration,
                start_seconds=cur_start,
                end_seconds=cur_end,
                verdict_history=history,
            )
        if verdict.verdict == "false_positive":
            # Delete the cut file — no false positives written to disk.
            # (Reverted 2026-07-08 ground-truth override — the correct
            # response to false_positive on a ground-truth source is to
            # let the retry loop's `fixable` path shift the window, not
            # to auto-pass a bad cut. If the VLM said false_positive
            # after max_iter iterations, the anchor is genuinely wrong
            # for THIS particular event; better to drop it than ship
            # aftermath.)
            out_path.unlink(missing_ok=True)
            return ClipLoopResult(
                ok=False,
                out_path=None,
                final_verdict=verdict,
                iterations=iteration,
                start_seconds=cur_start,
                end_seconds=cur_end,
                verdict_history=history,
            )
        # fixable → shift window + retry
        cur_start, cur_end = _apply_window_fix(cur_start, cur_end, duration, verdict)

    # Ran out of iterations — keep the last cut but mark it "cap reached"
    _log.info("vlm-per-clip-loop cap reached (%d iterations)", max_iter)
    return ClipLoopResult(
        ok=True,  # cap-reached still ships the clip; only false_positive drops it
        out_path=out_path,
        final_verdict=last_verdict
        or ClipVerdict(verdict="pass", why="cap_reached: no verdict"),
        iterations=max_iter,
        start_seconds=cur_start,
        end_seconds=cur_end,
        verdict_history=history,
    )


@observe(name="vlm-per-clip-iter")
def _validate_clip_iter(
    iteration: int,
    **kwargs,
) -> ClipVerdict:
    """Thin @observe-wrapper so each iteration is its own trace node."""
    return validate_clip(**kwargs)


# ---------------------------------------------------------------------
# Whole-compilation loop
# ---------------------------------------------------------------------


@dataclass
class CompLoopResult:
    """Outcome of the whole-comp review + fix loop."""

    ok: bool
    """True if the loop ran to completion (with or without fixes)."""

    passes: int
    """How many review passes fired."""

    applied_fixes: list[CompilationFix]
    """All fixes actually applied to the spec across passes."""

    unapplied_fixes: list[CompilationFix]
    """Fixes the VLM suggested but we couldn't safely apply (e.g. an
    apply_zoom fix pointing at an ROI we don't recognize)."""

    final_review: CompilationReview
    """The last review returned by the VLM."""


@observe(name="vlm-whole-comp-loop")
def review_and_apply_compilation(
    *,
    compilation_id: str,
    compilation_path: Path,
    game: str | None,
    clip_count: int,
    backend: VLMBackend | None = None,
    max_passes: int | None = None,
    n_frames: int | None = None,
    apply_fixes_fn=None,
) -> CompLoopResult:
    """Sample → review → apply → re-render, up to `max_passes`.

    Fix application is delegated to `apply_fixes_fn(compilation_id,
    fixes)` — the caller wires this to whatever mutator surface it
    prefers (in tests, a mock; in production, a helper that calls the
    pure `compile.py` mutators + `render_spec`). Returning the applied
    and unapplied fix lists keeps this loop backend-agnostic.

    If `apply_fixes_fn` is None the loop runs a single review pass and
    returns the fixes without applying any — useful for the
    "review only, don't touch" MCP mode.

    `apply_fixes_fn` must return an object with `applied` and
    `unapplied` list attributes (or a dict with those keys) plus,
    ideally, `new_path` if the re-render moved the compilation file.
    """
    backend = backend or select_backend()
    max_passes = max_passes or settings.vlm_max_comp_iter
    n_frames = n_frames or settings.vlm_frame_samples_comp

    applied: list[CompilationFix] = []
    unapplied: list[CompilationFix] = []
    last_review: CompilationReview | None = None
    cur_path = Path(compilation_path)
    cur_clip_count = clip_count

    for pass_num in range(1, max_passes + 1):
        review = _review_comp_pass(
            pass_num,
            compilation_path=cur_path,
            game=game,
            clip_count=cur_clip_count,
            n_frames=n_frames,
            backend=backend,
        )
        last_review = review

        if review.is_cohesive or not review.fixes:
            return CompLoopResult(
                ok=True,
                passes=pass_num,
                applied_fixes=applied,
                unapplied_fixes=unapplied,
                final_review=review,
            )
        if apply_fixes_fn is None:
            # Review-only mode: return the suggestions without applying.
            unapplied.extend(review.fixes)
            return CompLoopResult(
                ok=True,
                passes=pass_num,
                applied_fixes=applied,
                unapplied_fixes=unapplied,
                final_review=review,
            )

        result = apply_fixes_fn(compilation_id, review.fixes)
        applied_now = _get(result, "applied") or []
        unapplied_now = _get(result, "unapplied") or []
        applied.extend(applied_now)
        unapplied.extend(unapplied_now)
        # Follow any path change from the re-render.
        new_path = _get(result, "new_path")
        if new_path:
            cur_path = Path(new_path)
        new_count = _get(result, "clip_count")
        if isinstance(new_count, int):
            cur_clip_count = new_count

    # Cap reached — still return the final review as the loop's summary
    return CompLoopResult(
        ok=True,
        passes=max_passes,
        applied_fixes=applied,
        unapplied_fixes=unapplied,
        final_review=last_review or CompilationReview(is_cohesive=True, fixes=[]),
    )


def _get(obj, key: str):
    """Fetch attribute or dict key — apply_fixes_fn can return either."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


@observe(name="vlm-whole-comp-pass")
def _review_comp_pass(
    pass_num: int,
    *,
    compilation_path: Path,
    game: str | None,
    clip_count: int,
    n_frames: int,
    backend: VLMBackend,
) -> CompilationReview:
    """Thin @observe-wrapper — one pass = one trace node."""
    if not compilation_path.is_file():
        return CompilationReview(is_cohesive=True, fixes=[])
    duration = get_duration_seconds(str(compilation_path))
    return validate_compilation(
        compilation_path,
        game=game,
        clip_count=clip_count,
        total_duration_seconds=duration,
        n_frames=n_frames,
        backend=backend,
    )


# ---------------------------------------------------------------------
# Watch-montage refine loop -- panel-packed critic + drop-and-re-render
# ---------------------------------------------------------------------


from collections.abc import Callable, Sequence  # noqa: E402

from .watch_montage import MomentFlag, MontageCritique, WatchResult, watch_montage  # noqa: E402

# Flag categories that trigger dropping the clip they land in.
# Positive categories (great_moment, payoff_lands, good_buildup) protect
# a clip from being dropped even if a negative flag also lands on it.
_NEGATIVE_CATEGORIES = frozenset(
    {"dead_air", "scoreboard", "own_death", "teammate_kill_only", "boring_pacing"}
)
_POSITIVE_CATEGORIES = frozenset({"great_moment", "payoff_lands", "good_buildup"})


@dataclass
class WatchLoopResult:
    """Outcome of the watch-montage refine loop.

    `ok` is True on any completion that produced a rendered file
    (including cap_reached and no_drops_possible -- we still ship the
    last render). False only when the initial render itself failed.
    """

    ok: bool
    passes: int
    final_verdict: str
    """One of `ship_it`, `needs_edits`, `start_over`, or `vlm_unavailable`."""

    stopped_reason: str
    """Loop termination cause -- ship_it, start_over, cap_reached,
    no_drops_possible, would_empty_plan, vlm_unavailable, render_failed."""

    dropped_clips: list[str] = field(default_factory=list)
    """File names of clips dropped across all passes, in order."""

    final_critique: MontageCritique | None = None
    render_error: str | None = None


def _clip_indices_to_drop(
    flags: Sequence[MomentFlag],
    clip_timeline: Sequence[tuple[float, float]],
) -> list[int]:
    """Map flags to 0-based clip indices to drop.

    Rule: a clip is dropped if any flag landing in its montage-time
    range has a negative category AND no flag in that same range has a
    positive category. This means one great_moment flag protects the
    entire clip even if a scoreboard flag also lands there.

    `clip_timeline` is (t_start, t_end) per clip in the OUTPUT montage
    timeline (not source timeline).
    """
    negatives: set[int] = set()
    positives: set[int] = set()
    for f in flags:
        for i, (t0, t1) in enumerate(clip_timeline):
            # Overlap check -- flag interval intersects clip interval.
            if f.end_s > t0 and f.start_s < t1:
                if f.category in _NEGATIVE_CATEGORIES:
                    negatives.add(i)
                elif f.category in _POSITIVE_CATEGORIES:
                    positives.add(i)
    return sorted(negatives - positives)


def _clip_timeline_from_durations(durations: Sequence[float]) -> list[tuple[float, float]]:
    """Given per-clip durations, compute cumulative (start, end) ranges
    in output-montage time. Pure -- no ffprobe needed."""
    out: list[tuple[float, float]] = []
    t = 0.0
    for d in durations:
        end = t + max(0.0, float(d))
        out.append((t, end))
        t = end
    return out


@observe(name="vlm-watch-refine-loop")
def watch_and_refine_montage(
    *,
    initial_render: Callable[[], tuple[bool, str | None, Path]],
    re_render_after_drop: Callable[[list[int]], tuple[bool, str | None, Path]],
    clip_durations: Sequence[float],
    clip_names: Sequence[str],
    max_passes: int = 5,
    watch_fn: Callable[[str | Path], WatchResult] | None = None,
) -> WatchLoopResult:
    """Watch -> flag -> drop -> re-render loop.

    Callbacks isolate this module from the concrete render pipeline:

    - `initial_render()` -> renders the first cut and returns
      (ok, err, out_path). Called once at pass 1.
    - `re_render_after_drop(kept_indices)` -> re-renders using only the
      clips at those 0-based positions in the original list. Returns
      (ok, err, out_path). Called on each pass after clips are dropped.
    - `clip_durations` -- output-timeline duration of each clip in the
      ORIGINAL plan (used to map flag timestamps back to clip indices).
      Must be aligned with `clip_names`.
    - `clip_names` -- for reporting which clips got dropped.

    `watch_fn` defaults to `watch_montage` but can be injected for tests.
    """
    watch_fn = watch_fn or watch_montage
    kept: list[int] = list(range(len(clip_durations)))
    dropped_names: list[str] = []
    last_critique: MontageCritique | None = None

    for pass_num in range(1, max_passes + 1):
        if pass_num == 1:
            ok, err, out_path = initial_render()
        else:
            ok, err, out_path = re_render_after_drop(kept)
        if not ok:
            return WatchLoopResult(
                ok=False,
                passes=pass_num,
                final_verdict="render_failed",
                stopped_reason="render_failed",
                dropped_clips=dropped_names,
                final_critique=last_critique,
                render_error=err,
            )

        result = _watch_refine_pass(pass_num, out_path, watch_fn)
        critique = result.critique
        last_critique = critique
        if critique is None:
            return WatchLoopResult(
                ok=True,
                passes=pass_num,
                final_verdict="vlm_unavailable",
                stopped_reason="vlm_unavailable",
                dropped_clips=dropped_names,
                final_critique=None,
                render_error=result.error,
            )

        if critique.overall_verdict == "ship_it":
            return WatchLoopResult(
                ok=True,
                passes=pass_num,
                final_verdict="ship_it",
                stopped_reason="ship_it",
                dropped_clips=dropped_names,
                final_critique=critique,
            )
        if critique.overall_verdict == "start_over":
            # No amount of clip-dropping saves a fundamentally broken
            # input -- bail rather than churn to the cap.
            return WatchLoopResult(
                ok=True,
                passes=pass_num,
                final_verdict="start_over",
                stopped_reason="start_over",
                dropped_clips=dropped_names,
                final_critique=critique,
            )

        # needs_edits -- translate flags to clip drops.
        # Build a timeline for the CURRENT `kept` subset.
        current_durations = [clip_durations[i] for i in kept]
        timeline = _clip_timeline_from_durations(current_durations)
        drop_in_kept = _clip_indices_to_drop(critique.flags, timeline)

        if not drop_in_kept:
            return WatchLoopResult(
                ok=True,
                passes=pass_num,
                final_verdict="needs_edits",
                stopped_reason="no_drops_possible",
                dropped_clips=dropped_names,
                final_critique=critique,
            )

        # Guardrail -- never let the loop empty the plan or drop it
        # below 2 clips. Better to ship a montage with a scoreboard
        # than to ship one clip.
        remaining = len(kept) - len(drop_in_kept)
        if remaining < 2:
            return WatchLoopResult(
                ok=True,
                passes=pass_num,
                final_verdict="needs_edits",
                stopped_reason="would_empty_plan",
                dropped_clips=dropped_names,
                final_critique=critique,
            )

        # Translate positions in `kept` back to original clip indices,
        # record names, update kept.
        drop_originals = {kept[i] for i in drop_in_kept}
        for i in sorted(drop_originals):
            dropped_names.append(clip_names[i])
        kept = [i for i in kept if i not in drop_originals]

    return WatchLoopResult(
        ok=True,
        passes=max_passes,
        final_verdict="needs_edits",
        stopped_reason="cap_reached",
        dropped_clips=dropped_names,
        final_critique=last_critique,
    )


@observe(name="vlm-watch-refine-pass")
def _watch_refine_pass(
    pass_num: int,
    out_path: Path,
    watch_fn: Callable[[str | Path], WatchResult],
) -> WatchResult:
    """Thin @observe wrapper -- one pass = one Langfuse span. Argument
    `pass_num` is captured in the observation for tree readability."""
    _ = pass_num
    return watch_fn(out_path)
