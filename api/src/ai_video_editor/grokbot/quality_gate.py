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

Experiment (off by default): `GROKBOT_QUALITY_GATE_JEV_FRAMES_ENABLED` sends the
sampled frames to Jev directly (skips Haiku description). Shadow mode runs both
paths and keeps the Haiku verdict. Image errors fall back to the Haiku path.

Design: pure core (scoring logic) separated from I/O (VLM client, Jev).
Dependency injection for testability: tests use fakes, no real model calls.

Privacy: in-game names in frames are allowed. Never quote other players' chat
or voice; this path carries no transcripts.
"""

from __future__ import annotations

import base64
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import BaseModel

from ..config import settings
from ..edit_plan import QualityCriterion

if TYPE_CHECKING:
    from ..decisions.backends.jev import JevTransport
    from ..vlm.backends.base import VLMBackend

_log = logging.getLogger(__name__)

# TypeSafe Jev image limits (inclusive). Docs: https://docs.typesafe.ai/images
_JEV_MIN_SIDE = 280
_JEV_MAX_SIDE = 1568


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

        items = []
        for _, criterion in to_ask:
            if criterion.audio_led:
                state = {
                    "criterion": criterion.text,
                    "vision_description": "",
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
            items.append((BoolQuestion(prompt=prompt), state))

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
    # Hook within first N seconds
    if match := _TIMING_PATTERNS["hook_within"].match(criterion_id):
        threshold = int(match.group(1))
        has_hook = any(
            w.get("has_hook") and w.get("start_s", 0) <= threshold for w in metadata.clip_windows
        )
        found_msg = "found" if has_hook else "not found"
        return CriterionResult(
            criterion_id=criterion_id,
            verdict=Verdict.PASS if has_hook else Verdict.FAIL,
            confidence=1.0,
            reason=f"timing check: hook {found_msg} in first {threshold}s",
        )

    # No dead air over N seconds
    if match := _TIMING_PATTERNS["no_dead_air_over"].match(criterion_id):
        threshold = int(match.group(1))
        windows = sorted(metadata.clip_windows, key=lambda w: w.get("start_s", 0))
        max_gap = 0.0
        for i in range(1, len(windows)):
            gap = windows[i].get("start_s", 0) - windows[i - 1].get("end_s", 0)
            max_gap = max(max_gap, gap)
        has_dead_air = max_gap > threshold
        return CriterionResult(
            criterion_id=criterion_id,
            verdict=Verdict.FAIL if has_dead_air else Verdict.PASS,
            confidence=1.0,
            reason=f"timing check: max gap {max_gap:.1f}s, threshold {threshold}s",
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


def prepare_jev_frame(src: Path, dest: Path) -> Path:
    """Resize/pad a frame into TypeSafe's 280-1568 px per-side window.

    Uses Pillow (already used by the VLM montage path). No new dependency.
    """
    from PIL import Image

    with Image.open(src) as original:
        img = original.convert("RGB")
        # Shrink longest side into the max box (thumbnail never upscales).
        img.thumbnail((_JEV_MAX_SIDE, _JEV_MAX_SIDE), Image.Resampling.LANCZOS)
        w, h = img.size
        if w < _JEV_MIN_SIDE or h < _JEV_MIN_SIDE:
            scale = max(_JEV_MIN_SIDE / max(w, 1), _JEV_MIN_SIDE / max(h, 1))
            new_w = max(_JEV_MIN_SIDE, min(_JEV_MAX_SIDE, round(w * scale)))
            new_h = max(_JEV_MIN_SIDE, min(_JEV_MAX_SIDE, round(h * scale)))
            if max(new_w, new_h) > _JEV_MAX_SIDE or min(w * scale, h * scale) < _JEV_MIN_SIDE:
                # Letterbox onto a min-side canvas when aspect can't satisfy both.
                canvas_w = max(w, _JEV_MIN_SIDE)
                canvas_h = max(h, _JEV_MIN_SIDE)
                canvas_w = min(canvas_w, _JEV_MAX_SIDE)
                canvas_h = min(canvas_h, _JEV_MAX_SIDE)
                canvas = Image.new("RGB", (canvas_w, canvas_h), color=(0, 0, 0))
                canvas.paste(img, ((canvas_w - w) // 2, (canvas_h - h) // 2))
                img = canvas
            else:
                img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
        dest.parent.mkdir(parents=True, exist_ok=True)
        img.save(dest, format="JPEG", quality=90, optimize=True)
    return dest


def prepare_jev_frames(frames: list[Path], scratch_dir: Path) -> list[Path]:
    """Prepare up to 6 frames for a Jev image request."""
    out_dir = scratch_dir / "jev_frames"
    prepared: list[Path] = []
    for i, frame in enumerate(frames[: settings.grokbot_quality_gate_max_frames]):
        dest = out_dir / f"jev_frame_{i:03d}.jpg"
        try:
            prepared.append(prepare_jev_frame(frame, dest))
        except Exception as exc:
            _log.warning("quality_gate: prepare_jev_frame failed for %s: %s", frame, exc)
            raise
    return prepared


def _frame_assets(frames: list[Path]) -> tuple[dict[str, dict], list[dict]]:
    """Build TypeSafe assets map + state frame entries with UUID references."""
    assets: dict[str, dict] = {}
    frame_entries: list[dict] = []
    for i, path in enumerate(frames):
        ref = f"{uuid.uuid4()}-f{i}"
        data = base64.b64encode(path.read_bytes()).decode("ascii")
        fmt = "png" if path.suffix.lower() == ".png" else "jpg"
        assets[ref] = {"type": "image", "format": fmt, "data_base64": data}
        frame_entries.append({"index": i, "frame": ref})
    return assets, frame_entries


def _clip_json(metadata: RenderMetadata) -> dict[str, Any]:
    """Clip JSON for Jev state. No transcripts; captions are plan paraphrases."""
    return {
        "duration_seconds": metadata.duration_seconds,
        "aspect_ratio": metadata.aspect_ratio,
        "digest_moment_labels": list(metadata.digest_moment_labels),
        "captions": list(metadata.captions),
        "game": metadata.game,
        "clip_windows": list(metadata.clip_windows),
    }


def _answer_to_criterion_result(
    criterion: QualityCriterion,
    *,
    value: bool | None,
    probability: float,
    backend: str,
) -> CriterionResult:
    """Map a bool answer onto pass/fail/unsure with the gate threshold."""
    if backend == "rules":
        return CriterionResult(
            criterion_id=criterion.id,
            verdict=Verdict.UNSURE,
            confidence=0.0,
            reason="rules fallback: Jev unavailable",
        )
    if value is True:
        verdict = Verdict.PASS
    elif value is False:
        threshold = settings.grokbot_quality_gate_confidence_threshold
        verdict = Verdict.FAIL if probability >= threshold else Verdict.UNSURE
    else:
        verdict = Verdict.UNSURE

    # Audio-led criteria are never failed on frames alone.
    if criterion.audio_led and verdict == Verdict.FAIL:
        return CriterionResult(
            criterion_id=criterion.id,
            verdict=Verdict.UNSURE,
            confidence=probability,
            reason="audio-led: frames alone cannot fail; routed as unsure",
        )

    return CriterionResult(
        criterion_id=criterion.id,
        verdict=verdict,
        confidence=probability,
        reason=f"Jev ({backend}): p={probability:.2f}",
    )


def _maybe_cost_log(event: str, payload: dict[str, Any]) -> None:
    """Write to cost_log when that module exists on the branch."""
    try:
        from .. import cost_log as cost_log_mod
    except ImportError:
        return
    writer = getattr(cost_log_mod, "log_event", None) or getattr(cost_log_mod, "record", None)
    if callable(writer):
        try:
            writer(event, payload)
        except Exception as exc:
            _log.debug("quality_gate cost_log write failed: %s", exc)


@dataclass
class FramesScoreBundle:
    """Results + observability from a frames-to-Jev scoring pass."""

    results: list[CriterionResult]
    latency_ms: int = 0
    input_tokens: int | None = None
    error: str | None = None


class FramesJevScorer:
    """Score criteria by sending prepared frames + clip JSON to Jev in one request.

    Skips the Haiku vision description. Uses HttpTransport assets + Danger-Zone
    header. Audio-led fails are forced to UNSURE. Errors surface as UNSURE.
    """

    def __init__(self, transport: JevTransport | None = None) -> None:
        self._transport = transport

    def _get_transport(self) -> JevTransport:
        if self._transport is not None:
            return self._transport
        from ..decisions.backends.jev import default_transport

        transport = default_transport()
        if transport is None:
            raise RuntimeError("Jev is not configured: set TYPESAFE_API_KEY")
        return transport

    def score_all(
        self,
        criteria: list[QualityCriterion],
        frames: list[Path],
        metadata: RenderMetadata,
        *,
        already_passed: set[str] | None = None,
    ) -> FramesScoreBundle:
        from ..decisions.backends.jev import from_jev_answer, to_jev_question
        from ..decisions.models import BoolQuestion

        already_passed = already_passed or set()
        results: list[CriterionResult | None] = [None] * len(criteria)
        to_ask: list[tuple[int, QualityCriterion]] = []

        for i, criterion in enumerate(criteria):
            if criterion.id in already_passed:
                results[i] = CriterionResult(
                    criterion_id=criterion.id,
                    verdict=Verdict.PASS,
                    confidence=1.0,
                    reason="previously passed",
                )
            else:
                to_ask.append((i, criterion))

        if not to_ask:
            return FramesScoreBundle(results=[r for r in results if r is not None])

        prepared_error: str | None = None
        try:
            # Caller may pass already-prepared paths; re-prepare is idempotent enough
            # when files are JPEGs in range, but we always go through prepare for safety.
            scratch = frames[0].parent if frames else Path(".")
            prepared = prepare_jev_frames(frames, scratch)
            assets, frame_entries = _frame_assets(prepared)
            state = {
                "clip": _clip_json(metadata),
                "frames": frame_entries,
            }
            questions: dict[str, dict] = {}
            question_objs: dict[str, BoolQuestion] = {}
            for qi, (_idx, criterion) in enumerate(to_ask):
                key = f"q{qi}"
                if criterion.audio_led:
                    prompt = (
                        f"Using only `clip` metadata (digest_moment_labels, captions, "
                        f"duration) — ignore `frames` — does this video meet: "
                        f"'{criterion.text}'?"
                    )
                else:
                    prompt = (
                        f"Looking at `frames` and `clip`, does this video meet the "
                        f"criterion: '{criterion.text}'?"
                    )
                question_objs[key] = BoolQuestion(prompt=prompt)
                questions[key] = to_jev_question(question_objs[key])
        except Exception as exc:
            prepared_error = f"image prepare error: {exc}"
            _log.warning("quality_gate frames: %s", prepared_error)
            for idx, criterion in to_ask:
                results[idx] = CriterionResult(
                    criterion_id=criterion.id,
                    verdict=Verdict.UNSURE,
                    confidence=0.0,
                    reason=prepared_error,
                )
            return FramesScoreBundle(
                results=[r for r in results if r is not None],
                error=prepared_error,
            )

        transport = self._get_transport()
        start = time.perf_counter()
        try:
            from ..anthropic_surface import SpendCapError, reserve_call

            try:
                reserve_call("jev", settings.typesafe_max_calls, "TYPESAFE_MAX_CALLS")
            except SpendCapError as exc:
                raise RuntimeError(str(exc)) from exc

            raw_answers = transport.ask(state, questions, assets=assets)
            latency_ms = int((time.perf_counter() - start) * 1000)
            usage = getattr(transport, "last_usage", None) or {}
            input_tokens = usage.get("input_tokens") if isinstance(usage, dict) else None

            for key, (idx, criterion) in zip(questions, to_ask, strict=True):
                if key not in raw_answers:
                    results[idx] = CriterionResult(
                        criterion_id=criterion.id,
                        verdict=Verdict.UNSURE,
                        confidence=0.0,
                        reason=f"jev reply missing {key!r}",
                    )
                    continue
                answer = from_jev_answer(question_objs[key], raw_answers[key])
                results[idx] = _answer_to_criterion_result(
                    criterion,
                    value=answer.value if isinstance(answer.value, bool) else None,
                    probability=answer.probability,
                    backend=answer.backend,
                )

            _log.info(
                "quality_gate_jev_frames scored=%d latency_ms=%d input_tokens=%s",
                len(to_ask),
                latency_ms,
                input_tokens,
            )
            return FramesScoreBundle(
                results=[r for r in results if r is not None],
                latency_ms=latency_ms,
                input_tokens=input_tokens if isinstance(input_tokens, int) else None,
            )
        except Exception as exc:
            latency_ms = int((time.perf_counter() - start) * 1000)
            err = f"frames jev error: {exc}"
            _log.warning("quality_gate frames scoring failed: %s", exc)
            for idx, criterion in to_ask:
                results[idx] = CriterionResult(
                    criterion_id=criterion.id,
                    verdict=Verdict.UNSURE,
                    confidence=0.0,
                    reason=err,
                )
            return FramesScoreBundle(
                results=[r for r in results if r is not None],
                latency_ms=latency_ms,
                error=err,
            )


def _log_shadow_comparison(
    *,
    haiku_results: list[CriterionResult],
    frames_bundle: FramesScoreBundle,
    haiku_latency_ms: int,
) -> None:
    """Structured log + optional cost_log for shadow mode agreement."""
    frames_by_id = {r.criterion_id: r for r in frames_bundle.results}
    agreement: dict[str, bool] = {}
    for hr in haiku_results:
        fr = frames_by_id.get(hr.criterion_id)
        agreement[hr.criterion_id] = fr is not None and fr.verdict == hr.verdict

    payload = {
        "event": "quality_gate_jev_frames_shadow",
        "agreement": agreement,
        "haiku_latency_ms": haiku_latency_ms,
        "frames_latency_ms": frames_bundle.latency_ms,
        "frames_input_tokens": frames_bundle.input_tokens,
        "frames_error": frames_bundle.error,
        "haiku_verdicts": {r.criterion_id: r.verdict.value for r in haiku_results},
        "frames_verdicts": {r.criterion_id: r.verdict.value for r in frames_bundle.results},
    }
    _log.info(
        "quality_gate_jev_frames_shadow agreement=%s haiku_latency_ms=%d "
        "frames_latency_ms=%d frames_input_tokens=%s frames_error=%s",
        agreement,
        haiku_latency_ms,
        frames_bundle.latency_ms,
        frames_bundle.input_tokens,
        frames_bundle.error,
    )
    _maybe_cost_log("quality_gate_jev_frames_shadow", payload)


def _score_haiku_path(
    *,
    frames: list[Path],
    jev_criteria: list[QualityCriterion],
    metadata: RenderMetadata,
    vision_client: VisionClient,
    batched_scorer: BatchedJevScorer,
    result: GateResult,
    start_time: float,
    timeout: float,
) -> tuple[list[CriterionResult], int]:
    """Haiku description + batched Jev. Returns (results, scoring_latency_ms)."""
    if time.monotonic() - start_time > timeout:
        result.skipped = True
        result.skip_reason = "timeout before vision"
        return [], 0

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

    if time.monotonic() - start_time > timeout:
        result.skipped = True
        result.skip_reason = "timeout before Jev scoring"
        return [], 0

    scoring_start = time.monotonic()
    jev_results = batched_scorer.score_all(jev_criteria, description, metadata)
    scoring_ms = int((time.monotonic() - scoring_start) * 1000)
    result.timings["scoring_ms"] = scoring_ms
    return jev_results, scoring_ms


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
    frames_scorer: FramesJevScorer | None = None,
    timeout_seconds: float | None = None,
) -> tuple[GateResult, list[RevisionRequest]]:
    """Run the quality gate on a rendered video.

    Returns (gate_result, revision_requests). Revision requests are only
    emitted for confident fails that haven't exhausted retries.

    Fail-safe: any error or timeout skips the gate with skipped=True.

    Args:
        already_passed: Set of criterion IDs that passed on a previous attempt.
            These criteria are not re-evaluated; they appear in the result as PASS.
        frames_scorer: Optional frames-to-Jev scorer (experiment / tests).

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
    frames_scorer = frames_scorer or FramesJevScorer()
    use_frames = settings.grokbot_quality_gate_jev_frames_enabled
    shadow = settings.grokbot_quality_gate_jev_frames_shadow

    result = GateResult(passed=True)
    revision_requests: list[RevisionRequest] = []

    try:
        # 1. Check timing criteria first (pure computation, no model calls)
        # Also handle already-passed criteria from previous attempts
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
                haiku_results: list[CriterionResult] = []
                haiku_latency_ms = 0
                frames_bundle: FramesScoreBundle | None = None
                used_frames_primary = False

                # Shadow always needs the Haiku path as the real result.
                # Frames-primary runs frames first; image errors fall back to Haiku.
                if shadow or not use_frames:
                    if time.monotonic() - start_time > timeout:
                        return GateResult(
                            passed=True, skipped=True, skip_reason="timeout before vision"
                        ), []
                    haiku_results, haiku_latency_ms = _score_haiku_path(
                        frames=frames,
                        jev_criteria=jev_criteria,
                        metadata=metadata,
                        vision_client=vision_client,
                        batched_scorer=batched_scorer,
                        result=result,
                        start_time=start_time,
                        timeout=timeout,
                    )

                if (use_frames or shadow) and not (result.skipped and not shadow):
                    frames_bundle = frames_scorer.score_all(
                        jev_criteria,
                        frames,
                        metadata,
                    )
                    result.timings["frames_jev_ms"] = frames_bundle.latency_ms
                    if frames_bundle.input_tokens is not None:
                        result.cost_estimate = {
                            **result.cost_estimate,
                            "frames_jev_input_tokens": frames_bundle.input_tokens,
                        }

                if shadow:
                    if frames_bundle is not None:
                        _log_shadow_comparison(
                            haiku_results=haiku_results,
                            frames_bundle=frames_bundle,
                            haiku_latency_ms=haiku_latency_ms,
                        )
                    chosen = haiku_results
                elif use_frames:
                    if frames_bundle is not None and frames_bundle.error is None:
                        chosen = frames_bundle.results
                        used_frames_primary = True
                        result.vision_description = ""
                    else:
                        # Fail-safe: image/Jev error → Haiku description path.
                        _log.warning(
                            "quality_gate: frames path failed (%s); falling back to Haiku",
                            frames_bundle.error if frames_bundle else "no bundle",
                        )
                        if not haiku_results:
                            if time.monotonic() - start_time > timeout:
                                return GateResult(
                                    passed=True,
                                    skipped=True,
                                    skip_reason="timeout before Haiku fallback",
                                ), []
                            haiku_results, haiku_latency_ms = _score_haiku_path(
                                frames=frames,
                                jev_criteria=jev_criteria,
                                metadata=metadata,
                                vision_client=vision_client,
                                batched_scorer=batched_scorer,
                                result=result,
                                start_time=start_time,
                                timeout=timeout,
                            )
                        chosen = haiku_results
                else:
                    chosen = haiku_results

                result.timings["scoring_path"] = (
                    "jev_frames" if used_frames_primary else "haiku_description"
                )
                if len(chosen) == len(jev_criteria):
                    for criterion, cr in zip(jev_criteria, chosen, strict=True):
                        cr.retry_count = retry_counts.get(criterion.id, 0)
                        result.criteria_results.append(cr)

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
