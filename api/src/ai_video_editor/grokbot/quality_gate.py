"""Quality gate: optional post-render check using VLM vision + Jev scoring.

The gate is OFF by default (`GROKBOT_QUALITY_GATE_ENABLED=false`) so the existing
render flow is unchanged. Additionally, `GROKBOT_QUALITY_GATE_DAILY_CAP=0` or
negative also disables the gate. When enabled:

1. Sample at most 6 frames from the render at max 512px
2. One VLM call produces a text description of the video
3. Timing criteria (dead air, hook timing, duration) are checked in pure code
4. One batched Jev request scores all judgment criteria
5. Audio-led criteria: scored from metadata ONLY (never fail on frames)
6. Rules-backend fallback or errors count as unsure, never pass
7. Only confident fails trigger a revision request (max 2 retries/criterion)
8. Errors/timeouts skip the gate and proceed to owner approval

Design: pure core (scoring logic) separated from I/O (VLM client, Jev).
Dependency injection for testability: tests use fakes, no real model calls.
"""

from __future__ import annotations

import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel

from ..config import settings
from ..edit_plan import QualityCriterion

if TYPE_CHECKING:
    from ..vlm.backends.base import VLMBackend

_log = logging.getLogger(__name__)

_CRITERION_FENCE_OPEN = "<<<criterion>>>"
_CRITERION_FENCE_CLOSE = "<<<end_criterion>>>"


def _fenced_criterion_question(text: str) -> str:
    """Ask about a criterion without leaving its text unfenced in the prompt."""
    return (
        "Does this video meet the criterion between the markers? "
        f"{_CRITERION_FENCE_OPEN}{text}{_CRITERION_FENCE_CLOSE} "
        "Answer based only on the provided state."
    )


def _gate_state_dir() -> Path:
    path = Path(settings.workspace_dir) / "quality_gate"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _daily_cap_path() -> Path:
    return _gate_state_dir() / "daily_cap.json"


def _retry_state_path(edit_plan_id: str) -> Path:
    # edit_plan_id is already pattern-limited when coming from the queue.
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", edit_plan_id)[:64]
    return _gate_state_dir() / f"retry_{safe}.json"


def load_retry_state(edit_plan_id: str) -> tuple[dict[str, int], set[str]]:
    """Load persisted retry counts and already-passed ids for a plan."""
    path = _retry_state_path(edit_plan_id)
    if not path.is_file():
        return {}, set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        counts = {str(k): int(v) for k, v in (data.get("retry_counts") or {}).items()}
        passed = {str(x) for x in (data.get("already_passed") or [])}
        return counts, passed
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {}, set()


def save_retry_state(
    edit_plan_id: str,
    *,
    retry_counts: dict[str, int],
    already_passed: set[str],
) -> None:
    path = _retry_state_path(edit_plan_id)
    payload = {
        "retry_counts": retry_counts,
        "already_passed": sorted(already_passed),
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def reserve_daily_cap() -> bool:
    """Reserve one UTC-day vision slot. False when the daily cap is reached.

    Cap <= 0 means the gate is disabled (caller should skip before calling).
    """
    cap = settings.grokbot_quality_gate_daily_cap
    if cap <= 0:
        return False
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = _daily_cap_path()
    data = {"day": today, "count": 0}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if loaded.get("day") == today:
                data = {"day": today, "count": int(loaded.get("count", 0))}
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass
    if data["count"] >= cap:
        return False
    data["count"] += 1
    path.write_text(json.dumps(data), encoding="utf-8")
    return True


class Verdict(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    UNSURE = "unsure"


class CriterionResult(BaseModel):
    """Result of scoring one criterion."""

    criterion_id: str
    verdict: Verdict
    confidence: float
    reason: str = ""
    retry_count: int = 0


class GateResult(BaseModel):
    """Full quality gate result for a render."""

    passed: bool
    criteria_results: list[CriterionResult] = []
    vision_description: str = ""
    skipped: bool = False
    skip_reason: str = ""
    retries_exhausted: list[str] = []
    timings: dict = {}
    cost_estimate: dict = {}


@dataclass
class RenderMetadata:
    """Metadata about the render for scoring context."""

    duration_seconds: float = 0.0
    aspect_ratio: str = ""
    clip_windows: list[dict] = field(default_factory=list)
    captions: list[str] = field(default_factory=list)
    digest_moment_labels: list[str] = field(default_factory=list)
    game: str | None = None
    caption_boxes: list[dict] = field(default_factory=list)


class VisionClient(Protocol):
    """Protocol for the vision client used by the gate."""

    def describe_video(
        self,
        frames: list[Path],
        *,
        system_prompt: str,
        user_prompt: str,
    ) -> tuple[str, dict]:
        """Return (description, usage_info). Usage_info has model, tokens, etc."""
        ...


class JevScorer(Protocol):
    """Protocol for the Jev scorer used by the gate."""

    def score_criterion(
        self,
        criterion: QualityCriterion,
        vision_description: str,
        metadata: RenderMetadata,
    ) -> CriterionResult:
        """Score one criterion against the vision description and metadata."""
        ...


_VISION_SYSTEM_PROMPT = """You are a video quality reviewer. Describe what you see in these frames
from a short gameplay video. Focus on:
- What is happening in the gameplay (kills, deaths, objectives, etc.)
- Visual quality and clarity
- Any obvious issues (black frames, artifacts, missing HUD elements)
- The overall pacing and flow suggested by the frame sequence

Be concise and factual. Do not include player names or quotes."""

_VISION_USER_PROMPT = (
    "Describe this {duration:.1f}s {aspect} gameplay video based on these "
    "{n_frames} sampled frames.\n"
    "Game: {game}\n"
    "Focus on what happens, visual quality, and any issues you notice."
)


class DefaultVisionClient:
    """VLM-based vision client using the existing Anthropic backend."""

    def __init__(self, backend: VLMBackend | None = None):
        self._backend = backend

    def _get_backend(self) -> VLMBackend:
        if self._backend is not None:
            return self._backend
        from ..vlm.client import select_backend

        return select_backend()

    def describe_video(
        self,
        frames: list[Path],
        *,
        system_prompt: str,
        user_prompt: str,
    ) -> tuple[str, dict]:
        backend = self._get_backend()
        start = time.perf_counter()
        try:
            raw = backend.call(
                frames=frames,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )
            latency_ms = int((time.perf_counter() - start) * 1000)
            model_name = getattr(backend, "model", backend.name)
            usage_info = {
                "model": model_name,
                "frames_sent": len(frames),
                "latency_ms": latency_ms,
            }
            _log.info(
                "quality_gate vision: model=%s frames=%d latency=%dms",
                model_name,
                len(frames),
                latency_ms,
            )
            return raw, usage_info
        except Exception as exc:
            _log.warning("quality_gate vision call failed: %s", exc)
            raise


class DefaultJevScorer:
    """Jev-based scorer using the existing decisions backend.

    IMPORTANT: Rules-backend fallback is treated as UNSURE, never PASS.
    A broken gate must not silently approve content.
    """

    def score_criterion(
        self,
        criterion: QualityCriterion,
        vision_description: str,
        metadata: RenderMetadata,
    ) -> CriterionResult:
        from ..decisions import ask
        from ..decisions.models import BoolQuestion

        state = {
            "vision_description": "" if criterion.audio_led else vision_description,
            "digest_moment_labels": metadata.digest_moment_labels,
            "captions": metadata.captions,
            "duration_seconds": metadata.duration_seconds,
            "aspect_ratio": metadata.aspect_ratio,
        }
        question = BoolQuestion(prompt=_fenced_criterion_question(criterion.text))

        start = time.perf_counter()
        try:
            answer = ask(question, state)
            latency_ms = int((time.perf_counter() - start) * 1000)

            # Rules fallback must NOT be treated as confident pass
            if answer.backend == "rules":
                _log.warning(
                    "quality_gate: rules fallback for %s, treating as unsure",
                    criterion.id,
                )
                return CriterionResult(
                    criterion_id=criterion.id,
                    verdict=Verdict.UNSURE,
                    confidence=0.0,
                    reason="rules fallback: Jev unavailable, routed to owner",
                )

            if answer.value is True:
                verdict = Verdict.PASS
            elif answer.value is False:
                threshold = settings.grokbot_quality_gate_confidence_threshold
                verdict = Verdict.FAIL if answer.probability >= threshold else Verdict.UNSURE
            else:
                verdict = Verdict.UNSURE

            _log.info(
                "quality_gate jev: criterion=%s backend=%s verdict=%s p=%.2f latency=%dms",
                criterion.id,
                answer.backend,
                verdict.value,
                answer.probability,
                latency_ms,
            )
            return CriterionResult(
                criterion_id=criterion.id,
                verdict=verdict,
                confidence=answer.probability,
                reason=f"Jev ({answer.backend}): p={answer.probability:.2f}",
            )
        except Exception as exc:
            _log.warning("quality_gate jev scoring failed for %s: %s", criterion.id, exc)
            return CriterionResult(
                criterion_id=criterion.id,
                verdict=Verdict.UNSURE,
                confidence=0.0,
                reason=f"scoring error: {exc}",
            )


class BatchedJevScorer:
    """Score all criteria in one batched Jev request per render.

    Uses ask_many to batch all questions into a single request.
    Rules-backend fallback is treated as UNSURE for all criteria.
    """

    def score_all(
        self,
        criteria: list[QualityCriterion],
        vision_description: str,
        metadata: RenderMetadata,
        *,
        already_passed: set[str] | None = None,
    ) -> list[CriterionResult]:
        """Score all criteria in one batched request.

        Args:
            criteria: List of criteria to score
            vision_description: VLM description of the video
            metadata: Render metadata
            already_passed: Set of criterion IDs that already passed (skip on retry)

        Returns:
            List of CriterionResult, one per criterion
        """
        from ..decisions import ask_many
        from ..decisions.models import BoolQuestion

        already_passed = already_passed or set()
        results: list[CriterionResult] = []
        to_ask: list[tuple[int, QualityCriterion]] = []

        for i, criterion in enumerate(criteria):
            if criterion.id in already_passed:
                results.append(
                    CriterionResult(
                        criterion_id=criterion.id,
                        verdict=Verdict.PASS,
                        confidence=1.0,
                        reason="previously passed",
                    )
                )
            else:
                to_ask.append((i, criterion))
                results.append(None)  # Placeholder

        if not to_ask:
            return results

        # One shared state for all visual criteria so Jev groups into one
        # request; criterion text lives only in the fenced question.
        shared_state = {
            "vision_description": vision_description,
            "digest_moment_labels": metadata.digest_moment_labels,
            "captions": metadata.captions,
            "duration_seconds": metadata.duration_seconds,
            "aspect_ratio": metadata.aspect_ratio,
        }
        audio_state = {
            "vision_description": "",
            "digest_moment_labels": metadata.digest_moment_labels,
            "captions": metadata.captions,
            "duration_seconds": metadata.duration_seconds,
            "aspect_ratio": metadata.aspect_ratio,
        }
        items = []
        for _, criterion in to_ask:
            state = audio_state if criterion.audio_led else shared_state
            items.append((BoolQuestion(prompt=_fenced_criterion_question(criterion.text)), state))

        start = time.perf_counter()
        try:
            answers = ask_many(items)
            latency_ms = int((time.perf_counter() - start) * 1000)

            for (idx, criterion), answer in zip(to_ask, answers, strict=True):
                if answer.backend == "rules":
                    _log.warning(
                        "quality_gate batch: rules fallback for %s",
                        criterion.id,
                    )
                    results[idx] = CriterionResult(
                        criterion_id=criterion.id,
                        verdict=Verdict.UNSURE,
                        confidence=0.0,
                        reason="rules fallback: Jev unavailable",
                    )
                else:
                    if answer.value is True:
                        verdict = Verdict.PASS
                    elif answer.value is False:
                        threshold = settings.grokbot_quality_gate_confidence_threshold
                        verdict = (
                            Verdict.FAIL if answer.probability >= threshold else Verdict.UNSURE
                        )
                    else:
                        verdict = Verdict.UNSURE

                    results[idx] = CriterionResult(
                        criterion_id=criterion.id,
                        verdict=verdict,
                        confidence=answer.probability,
                        reason=f"Jev ({answer.backend}): p={answer.probability:.2f}",
                    )

            _log.info(
                "quality_gate batch: scored %d criteria in %dms",
                len(to_ask),
                latency_ms,
            )

        except Exception as exc:
            _log.warning("quality_gate batch scoring failed: %s", exc)
            for idx, criterion in to_ask:
                results[idx] = CriterionResult(
                    criterion_id=criterion.id,
                    verdict=Verdict.UNSURE,
                    confidence=0.0,
                    reason=f"batch error: {exc}",
                )

        return results


@dataclass
class RevisionRequest:
    """Request to the Editor to revise a specific part."""

    criterion_id: str
    criterion_text: str
    reason: str
    retry_number: int


# --- Timing checks: pure functions, no Jev ---

_TIMING_PATTERNS = {
    "hook_within": re.compile(r"hook_within_(\d+)s"),
    "no_dead_air_over": re.compile(r"no_dead_air_over_(\d+)s"),
    "duration_under": re.compile(r"duration_under_(\d+)s"),
    "duration_over": re.compile(r"duration_over_(\d+)s"),
}

# Safe area bounds by aspect ratio (y >= threshold means in safe area at bottom)
_CAPTION_SAFE_AREAS: dict[str, float] = {
    "9:16": 0.80,  # Portrait: safe area is bottom 20%
    "16:9": 0.75,  # Landscape: safe area is bottom 25%
    "1:1": 0.78,  # Square: safe area is bottom 22%
}


def check_timing_criteria(criterion_id: str, metadata: RenderMetadata) -> CriterionResult | None:
    """Check timing-based criteria using pure computation (no Jev).

    Returns CriterionResult if the criterion_id matches a timing pattern,
    otherwise None (indicating the criterion should go to Jev).
    """
    # Hook within first N seconds (requires has_hook on output/source windows)
    if match := _TIMING_PATTERNS["hook_within"].match(criterion_id):
        threshold = int(match.group(1))
        windows = metadata.clip_windows
        if not windows or not any("has_hook" in w for w in windows):
            return CriterionResult(
                criterion_id=criterion_id,
                verdict=Verdict.UNSURE,
                confidence=0.0,
                reason="timing check: missing has_hook on clip_windows",
            )
        has_hook = any(
            w.get("has_hook") and float(w.get("start_s", 0)) <= threshold for w in windows
        )
        found_msg = "found" if has_hook else "not found"
        return CriterionResult(
            criterion_id=criterion_id,
            verdict=Verdict.PASS if has_hook else Verdict.FAIL,
            confidence=1.0,
            reason=f"timing check: hook {found_msg} in first {threshold}s",
        )

    # No dead air over N seconds — only on output timeline fields
    if match := _TIMING_PATTERNS["no_dead_air_over"].match(criterion_id):
        threshold = int(match.group(1))
        windows = metadata.clip_windows
        if not windows or not all("output_start_s" in w and "output_end_s" in w for w in windows):
            return CriterionResult(
                criterion_id=criterion_id,
                verdict=Verdict.UNSURE,
                confidence=0.0,
                reason="timing check: missing output timeline for dead-air",
            )
        ordered = sorted(windows, key=lambda w: float(w.get("output_start_s", 0)))
        max_gap = 0.0
        for i in range(1, len(ordered)):
            gap = float(ordered[i]["output_start_s"]) - float(ordered[i - 1]["output_end_s"])
            max_gap = max(max_gap, gap)
        has_dead_air = max_gap > threshold
        return CriterionResult(
            criterion_id=criterion_id,
            verdict=Verdict.FAIL if has_dead_air else Verdict.PASS,
            confidence=1.0,
            reason=f"timing check: max output gap {max_gap:.1f}s, threshold {threshold}s",
        )

    # Duration under N seconds
    if match := _TIMING_PATTERNS["duration_under"].match(criterion_id):
        threshold = int(match.group(1))
        ok = metadata.duration_seconds < threshold
        return CriterionResult(
            criterion_id=criterion_id,
            verdict=Verdict.PASS if ok else Verdict.FAIL,
            confidence=1.0,
            reason=f"timing check: duration {metadata.duration_seconds:.1f}s, limit {threshold}s",
        )

    # Duration over N seconds
    if match := _TIMING_PATTERNS["duration_over"].match(criterion_id):
        threshold = int(match.group(1))
        ok = metadata.duration_seconds > threshold
        return CriterionResult(
            criterion_id=criterion_id,
            verdict=Verdict.PASS if ok else Verdict.FAIL,
            confidence=1.0,
            reason=f"timing check: duration {metadata.duration_seconds:.1f}s, minimum {threshold}s",
        )

    # Caption safe area check
    if criterion_id == "caption_safe_area":
        if not metadata.caption_boxes:
            _log.info("caption_safe_area: no caption geometry available, skipping")
            return CriterionResult(
                criterion_id=criterion_id,
                verdict=Verdict.UNSURE,
                confidence=0.0,
                reason="no caption geometry available",
            )

        # Get safe area threshold for aspect ratio (default to portrait if unknown)
        safe_y = _CAPTION_SAFE_AREAS.get(metadata.aspect_ratio, 0.80)

        # Check all caption boxes are in safe area
        all_safe = True
        for box in metadata.caption_boxes:
            box_y = box.get("y", 0.0)
            if box_y < safe_y:
                all_safe = False
                break

        return CriterionResult(
            criterion_id=criterion_id,
            verdict=Verdict.PASS if all_safe else Verdict.FAIL,
            confidence=1.0,
            reason=f"caption safe area: {'all in safe zone' if all_safe else 'outside safe zone'} "
            f"(aspect={metadata.aspect_ratio}, threshold y>={safe_y})",
        )

    return None


def _sample_frames(
    video_path: Path,
    scratch_dir: Path,
    duration_seconds: float,
    *,
    max_frames: int = 6,
    max_width: int = 512,
) -> list[Path]:
    """Sample frames from the video for vision analysis."""
    from ..vlm.frames import extract_frames

    n_frames = min(max_frames, settings.grokbot_quality_gate_max_frames)
    return extract_frames(
        str(video_path),
        scratch_dir,
        n_samples=n_frames,
        duration_seconds=duration_seconds,
        max_width=max_width,
    )


def run_quality_gate(
    video_path: Path,
    criteria: list[QualityCriterion],
    metadata: RenderMetadata,
    scratch_dir: Path,
    *,
    retry_counts: dict[str, int] | None = None,
    already_passed: set[str] | None = None,
    vision_client: VisionClient | None = None,
    jev_scorer: JevScorer | None = None,
    batched_scorer: BatchedJevScorer | None = None,
    timeout_seconds: float | None = None,
) -> tuple[GateResult, list[RevisionRequest]]:
    """Run the quality gate on a rendered video.

    Returns (gate_result, revision_requests). Revision requests are only
    emitted for confident fails that haven't exhausted retries.

    Fail-safe: any error or timeout skips the gate with skipped=True.

    Args:
        already_passed: Set of criterion IDs that passed on a previous attempt.
            These criteria are not re-evaluated; they appear in the result as PASS.

    Gate is disabled when:
    - GROKBOT_QUALITY_GATE_ENABLED=false
    - GROKBOT_QUALITY_GATE_DAILY_CAP <= 0
    """
    if not criteria:
        return GateResult(passed=True, skipped=True, skip_reason="no criteria"), []

    if not settings.grokbot_quality_gate_enabled:
        return GateResult(passed=True, skipped=True, skip_reason="gate disabled"), []

    # Daily cap of 0 or negative means gate disabled
    if settings.grokbot_quality_gate_daily_cap <= 0:
        return GateResult(
            passed=True, skipped=True, skip_reason="daily cap disabled (set to 0 or negative)"
        ), []

    timeout = timeout_seconds or settings.grokbot_quality_gate_timeout_seconds
    start_time = time.monotonic()
    retry_counts = retry_counts or {}
    already_passed = already_passed or set()
    vision_client = vision_client or DefaultVisionClient()
    batched_scorer = batched_scorer or BatchedJevScorer()

    result = GateResult(passed=True)
    revision_requests: list[RevisionRequest] = []

    try:
        # 1. Check timing criteria first (pure computation, no model calls)
        # Also handle already-passed criteria from previous attempts
        timing_results: list[CriterionResult] = []
        jev_criteria: list[QualityCriterion] = []

        for criterion in criteria:
            # Already-passed criteria are not re-evaluated
            if criterion.id in already_passed:
                result.criteria_results.append(
                    CriterionResult(
                        criterion_id=criterion.id,
                        verdict=Verdict.PASS,
                        confidence=1.0,
                        reason="previously passed",
                        retry_count=retry_counts.get(criterion.id, 0),
                    )
                )
                continue

            timing_result = check_timing_criteria(criterion.id, metadata)
            if timing_result is not None:
                timing_result.retry_count = retry_counts.get(criterion.id, 0)
                timing_results.append(timing_result)
                result.criteria_results.append(timing_result)
            else:
                jev_criteria.append(criterion)

        # 2. Sample frames (only if we have Jev criteria)
        if jev_criteria:
            frame_start = time.monotonic()
            frames = _sample_frames(
                video_path,
                scratch_dir,
                metadata.duration_seconds,
                max_frames=settings.grokbot_quality_gate_max_frames,
                max_width=settings.grokbot_quality_gate_frame_max_width,
            )
            result.timings["frame_sampling_ms"] = int((time.monotonic() - frame_start) * 1000)

            if not frames:
                _log.warning("quality_gate: no frames extracted, skipping Jev criteria")
                for criterion in jev_criteria:
                    result.criteria_results.append(
                        CriterionResult(
                            criterion_id=criterion.id,
                            verdict=Verdict.UNSURE,
                            confidence=0.0,
                            reason="no frames extracted",
                            retry_count=retry_counts.get(criterion.id, 0),
                        )
                    )
            else:
                # 3. Vision pass (bounded by remaining gate deadline)
                remaining = timeout - (time.monotonic() - start_time)
                if remaining <= 0:
                    return GateResult(
                        passed=True, skipped=True, skip_reason="timeout before vision"
                    ), []

                if not reserve_daily_cap():
                    return GateResult(
                        passed=True,
                        skipped=True,
                        skip_reason="daily cap reached",
                    ), []

                vision_start = time.monotonic()
                user_prompt = _VISION_USER_PROMPT.format(
                    duration=metadata.duration_seconds,
                    aspect=metadata.aspect_ratio,
                    n_frames=len(frames),
                    game=metadata.game or "unknown",
                )

                def _vision_call():
                    return vision_client.describe_video(
                        frames,
                        system_prompt=_VISION_SYSTEM_PROMPT,
                        user_prompt=user_prompt,
                    )

                with ThreadPoolExecutor(max_workers=1) as pool:
                    fut = pool.submit(_vision_call)
                    try:
                        description, usage_info = fut.result(timeout=remaining)
                    except FuturesTimeout:
                        return GateResult(
                            passed=True,
                            skipped=True,
                            skip_reason="timeout during vision",
                            timings={"total_ms": int((time.monotonic() - start_time) * 1000)},
                        ), []

                result.vision_description = description
                result.cost_estimate = usage_info
                result.timings["vision_ms"] = int((time.monotonic() - vision_start) * 1000)

                # 4. Score all Jev criteria in one batched request
                remaining = timeout - (time.monotonic() - start_time)
                if remaining <= 0:
                    result.skipped = True
                    result.skip_reason = "timeout before Jev scoring"
                else:
                    scoring_start = time.monotonic()

                    def _score_call():
                        return batched_scorer.score_all(
                            jev_criteria,
                            description,
                            metadata,
                            already_passed=already_passed,
                        )

                    with ThreadPoolExecutor(max_workers=1) as pool:
                        fut = pool.submit(_score_call)
                        try:
                            jev_results = fut.result(timeout=remaining)
                        except FuturesTimeout:
                            result.skipped = True
                            result.skip_reason = "timeout during Jev scoring"
                            jev_results = None

                    if jev_results is not None:
                        for criterion, cr in zip(jev_criteria, jev_results, strict=True):
                            cr.retry_count = retry_counts.get(criterion.id, 0)
                            result.criteria_results.append(cr)
                    result.timings["scoring_ms"] = int((time.monotonic() - scoring_start) * 1000)

        # 5. Process results and generate revision requests
        for cr in result.criteria_results:
            threshold = settings.grokbot_quality_gate_confidence_threshold
            is_confident_fail = cr.verdict == Verdict.FAIL and cr.confidence >= threshold
            if is_confident_fail:
                criterion = next((c for c in criteria if c.id == cr.criterion_id), None)
                if criterion and cr.retry_count < settings.grokbot_quality_gate_max_retries:
                    revision_requests.append(
                        RevisionRequest(
                            criterion_id=cr.criterion_id,
                            criterion_text=criterion.text,
                            reason=cr.reason,
                            retry_number=cr.retry_count + 1,
                        )
                    )
                    result.passed = False
                elif criterion:
                    result.retries_exhausted.append(cr.criterion_id)

        result.timings["total_ms"] = int((time.monotonic() - start_time) * 1000)

    except Exception as exc:
        _log.exception("quality_gate failed: %s", exc)
        return GateResult(
            passed=True,
            skipped=True,
            skip_reason=f"error: {exc}",
            timings={"total_ms": int((time.monotonic() - start_time) * 1000)},
        ), []

    return result, revision_requests
