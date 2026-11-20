"""Quality gate: optional post-render check using VLM vision + Jev scoring.

The gate is OFF by default (`GROKBOT_QUALITY_GATE_ENABLED=false`) so the existing
render flow is unchanged. When enabled:

1. Sample at most 6 frames from the render at max 512px
2. One VLM call produces a text description of the video
3. Jev scores the description + render metadata against each criterion
4. Visual-led criteria: scored from frames + metadata
5. Audio-led criteria: scored from metadata ONLY (never fail on frames)
6. Only confident fails trigger a revision request (max 2 retries/criterion)
7. Errors/timeouts skip the gate and proceed to owner approval

Design: pure core (scoring logic) separated from I/O (VLM client, Jev).
Dependency injection for testability: tests use fakes, no real model calls.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel

from ..config import settings
from ..edit_plan import QualityCriterion

if TYPE_CHECKING:
    from ..vlm.backends.base import VLMBackend

_log = logging.getLogger(__name__)


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
    """Jev-based scorer using the existing decisions backend."""

    def score_criterion(
        self,
        criterion: QualityCriterion,
        vision_description: str,
        metadata: RenderMetadata,
    ) -> CriterionResult:
        from ..decisions import ask
        from ..decisions.models import BoolQuestion

        if criterion.audio_led:
            state = {
                "criterion": criterion.text,
                "digest_moment_labels": metadata.digest_moment_labels,
                "captions": metadata.captions,
                "duration_seconds": metadata.duration_seconds,
            }
        else:
            state = {
                "criterion": criterion.text,
                "vision_description": vision_description,
                "digest_moment_labels": metadata.digest_moment_labels,
                "captions": metadata.captions,
                "duration_seconds": metadata.duration_seconds,
                "aspect_ratio": metadata.aspect_ratio,
            }

        prompt = f"Does this video meet the criterion: '{criterion.text}'?"
        question = BoolQuestion(prompt=prompt)

        start = time.perf_counter()
        try:
            answer = ask(question, state)
            latency_ms = int((time.perf_counter() - start) * 1000)
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


@dataclass
class RevisionRequest:
    """Request to the Editor to revise a specific part."""

    criterion_id: str
    criterion_text: str
    reason: str
    retry_number: int


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
    vision_client: VisionClient | None = None,
    jev_scorer: JevScorer | None = None,
    timeout_seconds: float | None = None,
) -> tuple[GateResult, list[RevisionRequest]]:
    """Run the quality gate on a rendered video.

    Returns (gate_result, revision_requests). Revision requests are only
    emitted for confident fails that haven't exhausted retries.

    Fail-safe: any error or timeout skips the gate with skipped=True.
    """
    if not criteria:
        return GateResult(passed=True, skipped=True, skip_reason="no criteria"), []

    if not settings.grokbot_quality_gate_enabled:
        return GateResult(passed=True, skipped=True, skip_reason="gate disabled"), []

    timeout = timeout_seconds or settings.grokbot_quality_gate_timeout_seconds
    start_time = time.monotonic()
    retry_counts = retry_counts or {}
    vision_client = vision_client or DefaultVisionClient()
    jev_scorer = jev_scorer or DefaultJevScorer()

    result = GateResult(passed=True)
    revision_requests: list[RevisionRequest] = []

    try:
        # 1. Sample frames
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
            _log.warning("quality_gate: no frames extracted, skipping")
            return GateResult(passed=True, skipped=True, skip_reason="no frames extracted"), []

        # 2. Vision pass
        if time.monotonic() - start_time > timeout:
            return GateResult(passed=True, skipped=True, skip_reason="timeout before vision"), []

        vision_start = time.monotonic()
        user_prompt = _VISION_USER_PROMPT.format(
            duration=metadata.duration_seconds,
            aspect=metadata.aspect_ratio,
            n_frames=len(frames),
            game=metadata.game or "unknown",
        )
        description, usage_info = vision_client.describe_video(
            frames,
            system_prompt=_VISION_SYSTEM_PROMPT,
            user_prompt=user_prompt,
        )
        result.vision_description = description
        result.cost_estimate = usage_info
        result.timings["vision_ms"] = int((time.monotonic() - vision_start) * 1000)

        # 3. Score each criterion
        scoring_start = time.monotonic()
        for criterion in criteria:
            if time.monotonic() - start_time > timeout:
                result.skipped = True
                result.skip_reason = "timeout during scoring"
                break

            current_retries = retry_counts.get(criterion.id, 0)
            criterion_result = jev_scorer.score_criterion(criterion, description, metadata)
            criterion_result.retry_count = current_retries
            result.criteria_results.append(criterion_result)

            threshold = settings.grokbot_quality_gate_confidence_threshold
            is_confident_fail = (
                criterion_result.verdict == Verdict.FAIL
                and criterion_result.confidence >= threshold
            )
            if is_confident_fail:
                if current_retries < settings.grokbot_quality_gate_max_retries:
                    revision_requests.append(
                        RevisionRequest(
                            criterion_id=criterion.id,
                            criterion_text=criterion.text,
                            reason=criterion_result.reason,
                            retry_number=current_retries + 1,
                        )
                    )
                    result.passed = False
                else:
                    result.retries_exhausted.append(criterion.id)

        result.timings["scoring_ms"] = int((time.monotonic() - scoring_start) * 1000)
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
