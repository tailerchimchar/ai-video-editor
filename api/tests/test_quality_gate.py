"""Quality gate tests: unit, integration, and e2e with fakes.

Tests cover:
- Pure scoring logic (criterion routing, confidence thresholds, retry cap)
- Integration with fake VLM and Jev clients
- Full gate loop with fake storage
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Set required env vars before importing modules that use settings
os.environ.setdefault("OUTPLAYED_MEDIA_DIR", "/tmp/fake_media")
os.environ.setdefault("WORKSPACE_DIR", "/tmp/fake_workspace")

from ai_video_editor.edit_plan import QualityCriterion
from ai_video_editor.grokbot.quality_gate import (
    CriterionResult,
    DefaultJevScorer,
    DefaultVisionClient,
    GateResult,
    RenderMetadata,
    RevisionRequest,
    Verdict,
    run_quality_gate,
)

# --- Fake implementations for testing ---


class FakeVisionClient:
    """Fake vision client that returns canned descriptions."""

    def __init__(self, description: str = "A kill happens at 5s", usage: dict | None = None):
        self.description = description
        self.usage = usage or {"model": "fake-vlm", "frames_sent": 6, "latency_ms": 100}
        self.calls: list[tuple[list[Path], str, str]] = []

    def describe_video(
        self, frames: list[Path], *, system_prompt: str, user_prompt: str
    ) -> tuple[str, dict]:
        self.calls.append((frames, system_prompt, user_prompt))
        return self.description, self.usage


class FakeJevScorer:
    """Fake Jev scorer that returns configurable results."""

    def __init__(self, results: dict[str, CriterionResult] | None = None):
        self._results = results or {}
        self.calls: list[tuple[QualityCriterion, str, RenderMetadata]] = []

    def score_criterion(
        self,
        criterion: QualityCriterion,
        vision_description: str,
        metadata: RenderMetadata,
    ) -> CriterionResult:
        self.calls.append((criterion, vision_description, metadata))
        if criterion.id in self._results:
            return self._results[criterion.id]
        return CriterionResult(
            criterion_id=criterion.id,
            verdict=Verdict.PASS,
            confidence=0.9,
            reason="fake pass",
        )


# --- Unit tests: pure scoring logic ---


class TestVerdict:
    def test_verdict_enum_values(self):
        assert Verdict.PASS.value == "pass"
        assert Verdict.FAIL.value == "fail"
        assert Verdict.UNSURE.value == "unsure"


class TestCriterionResult:
    def test_model_serialization(self):
        result = CriterionResult(
            criterion_id="c1",
            verdict=Verdict.PASS,
            confidence=0.95,
            reason="looks good",
        )
        data = result.model_dump()
        assert data["criterion_id"] == "c1"
        assert data["verdict"] == "pass"
        assert data["confidence"] == 0.95

    def test_retry_count_default(self):
        result = CriterionResult(
            criterion_id="c1",
            verdict=Verdict.FAIL,
            confidence=0.8,
        )
        assert result.retry_count == 0


class TestGateResult:
    def test_default_passed(self):
        result = GateResult(passed=True)
        assert result.passed
        assert result.criteria_results == []
        assert not result.skipped

    def test_skipped_result(self):
        result = GateResult(passed=True, skipped=True, skip_reason="gate disabled")
        assert result.skipped
        assert result.skip_reason == "gate disabled"


class TestRenderMetadata:
    def test_dataclass_defaults(self):
        meta = RenderMetadata()
        assert meta.duration_seconds == 0.0
        assert meta.aspect_ratio == ""
        assert meta.clip_windows == []
        assert meta.digest_moment_labels == []


class TestRevisionRequest:
    def test_dataclass_fields(self):
        req = RevisionRequest(
            criterion_id="c1",
            criterion_text="Clip should show a kill",
            reason="No kill visible",
            retry_number=1,
        )
        assert req.criterion_id == "c1"
        assert req.retry_number == 1


# --- Integration tests with fakes ---


@pytest.fixture
def test_video(tmp_path):
    """Create a minimal test video file."""
    video = tmp_path / "test.mp4"
    video.write_bytes(b"\x00" * 1000)
    return video


@pytest.fixture
def scratch_dir(tmp_path):
    """Create a scratch directory for frame extraction."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    return scratch


class TestRunQualityGate:
    def test_no_criteria_skips_gate(self, test_video, scratch_dir):
        result, revisions = run_quality_gate(
            test_video,
            [],
            RenderMetadata(),
            scratch_dir,
        )
        assert result.passed
        assert result.skipped
        assert "no criteria" in result.skip_reason
        assert revisions == []

    def test_gate_disabled_skips(self, test_video, scratch_dir, monkeypatch):
        from ai_video_editor import config

        monkeypatch.setattr(config.settings, "grokbot_quality_gate_enabled", False)

        result, _revisions = run_quality_gate(
            test_video,
            [QualityCriterion(id="c1", text="test")],
            RenderMetadata(),
            scratch_dir,
        )
        assert result.passed
        assert result.skipped
        assert "disabled" in result.skip_reason

    def test_all_criteria_pass(self, test_video, scratch_dir, monkeypatch):
        from ai_video_editor import config

        monkeypatch.setattr(config.settings, "grokbot_quality_gate_enabled", True)

        fake_vision = FakeVisionClient(description="Kill at 5s, good quality")
        fake_jev = FakeJevScorer()

        with patch(
            "ai_video_editor.grokbot.quality_gate._sample_frames",
            return_value=[scratch_dir / "frame_001.jpg"],
        ):
            (scratch_dir / "frame_001.jpg").write_bytes(b"\x00")

            result, revisions = run_quality_gate(
                test_video,
                [QualityCriterion(id="c1", text="Shows a kill")],
                RenderMetadata(duration_seconds=10.0, aspect_ratio="9:16"),
                scratch_dir,
                vision_client=fake_vision,
                jev_scorer=fake_jev,
            )

        assert result.passed
        assert not result.skipped
        assert len(result.criteria_results) == 1
        assert result.criteria_results[0].verdict == Verdict.PASS
        assert revisions == []

    def test_confident_fail_emits_revision(self, test_video, scratch_dir, monkeypatch):
        from ai_video_editor import config

        monkeypatch.setattr(config.settings, "grokbot_quality_gate_enabled", True)
        monkeypatch.setattr(config.settings, "grokbot_quality_gate_confidence_threshold", 0.7)
        monkeypatch.setattr(config.settings, "grokbot_quality_gate_max_retries", 2)

        fake_vision = FakeVisionClient(description="Scoreboard only, no action")
        fake_jev = FakeJevScorer(
            results={
                "c1": CriterionResult(
                    criterion_id="c1",
                    verdict=Verdict.FAIL,
                    confidence=0.85,
                    reason="No kill visible",
                )
            }
        )

        with patch(
            "ai_video_editor.grokbot.quality_gate._sample_frames",
            return_value=[scratch_dir / "frame_001.jpg"],
        ):
            (scratch_dir / "frame_001.jpg").write_bytes(b"\x00")

            result, revisions = run_quality_gate(
                test_video,
                [QualityCriterion(id="c1", text="Shows a kill")],
                RenderMetadata(duration_seconds=10.0),
                scratch_dir,
                vision_client=fake_vision,
                jev_scorer=fake_jev,
            )

        assert not result.passed
        assert len(revisions) == 1
        assert revisions[0].criterion_id == "c1"
        assert revisions[0].retry_number == 1

    def test_low_confidence_fail_does_not_emit_revision(self, test_video, scratch_dir, monkeypatch):
        from ai_video_editor import config

        monkeypatch.setattr(config.settings, "grokbot_quality_gate_enabled", True)
        monkeypatch.setattr(config.settings, "grokbot_quality_gate_confidence_threshold", 0.7)

        fake_vision = FakeVisionClient()
        fake_jev = FakeJevScorer(
            results={
                "c1": CriterionResult(
                    criterion_id="c1",
                    verdict=Verdict.FAIL,
                    confidence=0.5,
                    reason="Uncertain",
                )
            }
        )

        with patch(
            "ai_video_editor.grokbot.quality_gate._sample_frames",
            return_value=[scratch_dir / "frame_001.jpg"],
        ):
            (scratch_dir / "frame_001.jpg").write_bytes(b"\x00")

            result, revisions = run_quality_gate(
                test_video,
                [QualityCriterion(id="c1", text="test")],
                RenderMetadata(duration_seconds=10.0),
                scratch_dir,
                vision_client=fake_vision,
                jev_scorer=fake_jev,
            )

        assert result.passed
        assert revisions == []

    def test_retry_cap_exhausted(self, test_video, scratch_dir, monkeypatch):
        from ai_video_editor import config

        monkeypatch.setattr(config.settings, "grokbot_quality_gate_enabled", True)
        monkeypatch.setattr(config.settings, "grokbot_quality_gate_confidence_threshold", 0.7)
        monkeypatch.setattr(config.settings, "grokbot_quality_gate_max_retries", 2)

        fake_vision = FakeVisionClient()
        fake_jev = FakeJevScorer(
            results={
                "c1": CriterionResult(
                    criterion_id="c1",
                    verdict=Verdict.FAIL,
                    confidence=0.9,
                    reason="Still failing",
                )
            }
        )

        with patch(
            "ai_video_editor.grokbot.quality_gate._sample_frames",
            return_value=[scratch_dir / "frame_001.jpg"],
        ):
            (scratch_dir / "frame_001.jpg").write_bytes(b"\x00")

            result, revisions = run_quality_gate(
                test_video,
                [QualityCriterion(id="c1", text="test")],
                RenderMetadata(duration_seconds=10.0),
                scratch_dir,
                retry_counts={"c1": 2},
                vision_client=fake_vision,
                jev_scorer=fake_jev,
            )

        assert result.passed
        assert "c1" in result.retries_exhausted
        assert revisions == []


class TestAudioLedCriteria:
    def test_audio_led_uses_metadata_only(self, test_video, scratch_dir, monkeypatch):
        from ai_video_editor import config

        monkeypatch.setattr(config.settings, "grokbot_quality_gate_enabled", True)

        fake_vision = FakeVisionClient(description="Boring scoreboard")
        calls_recorded = []

        class RecordingJevScorer:
            def score_criterion(self, criterion, vision_description, metadata):
                calls_recorded.append(
                    {
                        "criterion": criterion,
                        "vision_description": vision_description,
                        "metadata": metadata,
                    }
                )
                return CriterionResult(
                    criterion_id=criterion.id,
                    verdict=Verdict.PASS,
                    confidence=0.9,
                )

        with patch(
            "ai_video_editor.grokbot.quality_gate._sample_frames",
            return_value=[scratch_dir / "frame_001.jpg"],
        ):
            (scratch_dir / "frame_001.jpg").write_bytes(b"\x00")

            run_quality_gate(
                test_video,
                [
                    QualityCriterion(id="visual", text="Shows action", audio_led=False),
                    QualityCriterion(id="audio", text="Has funny moment", audio_led=True),
                ],
                RenderMetadata(
                    duration_seconds=10.0,
                    digest_moment_labels=["funny_callout"],
                    captions=["That was hilarious!"],
                ),
                scratch_dir,
                vision_client=fake_vision,
                jev_scorer=RecordingJevScorer(),
            )

        assert len(calls_recorded) == 2
        visual_call = next(c for c in calls_recorded if c["criterion"].id == "visual")
        audio_call = next(c for c in calls_recorded if c["criterion"].id == "audio")

        assert visual_call["criterion"].audio_led is False
        assert audio_call["criterion"].audio_led is True


class TestVisionError:
    def test_vision_error_skips_gate(self, test_video, scratch_dir, monkeypatch):
        from ai_video_editor import config

        monkeypatch.setattr(config.settings, "grokbot_quality_gate_enabled", True)

        class FailingVisionClient:
            def describe_video(self, frames, *, system_prompt, user_prompt):
                raise RuntimeError("VLM unavailable")

        with patch(
            "ai_video_editor.grokbot.quality_gate._sample_frames",
            return_value=[scratch_dir / "frame_001.jpg"],
        ):
            (scratch_dir / "frame_001.jpg").write_bytes(b"\x00")

            result, revisions = run_quality_gate(
                test_video,
                [QualityCriterion(id="c1", text="test")],
                RenderMetadata(duration_seconds=10.0),
                scratch_dir,
                vision_client=FailingVisionClient(),
            )

        assert result.passed
        assert result.skipped
        assert "error" in result.skip_reason
        assert revisions == []


class TestNoFramesExtracted:
    def test_no_frames_skips_gate(self, test_video, scratch_dir, monkeypatch):
        from ai_video_editor import config

        monkeypatch.setattr(config.settings, "grokbot_quality_gate_enabled", True)

        with patch("ai_video_editor.grokbot.quality_gate._sample_frames", return_value=[]):
            result, _revisions = run_quality_gate(
                test_video,
                [QualityCriterion(id="c1", text="test")],
                RenderMetadata(duration_seconds=10.0),
                scratch_dir,
            )

        assert result.passed
        assert result.skipped
        assert "no frames" in result.skip_reason


# --- E2E tests with fake storage ---


class TestBoxRenderIntegration:
    def test_quality_gate_included_in_result(self, tmp_path, monkeypatch):
        """Quality gate results appear in the RenderResult."""
        from ai_video_editor import config
        from ai_video_editor.grokbot.box_render import RenderOutput, RenderResult

        monkeypatch.setattr(config.settings, "grokbot_quality_gate_enabled", True)

        result = RenderResult(
            ok=True,
            edit_plan_id="plan-001",
            edit_plan_version="1",
            asset_id="asset-001",
            outputs=[
                RenderOutput(
                    aspect="9:16",
                    local_path=tmp_path / "out.mp4",
                    duration_seconds=10.0,
                )
            ],
        )

        gate_result = GateResult(
            passed=True,
            criteria_results=[
                CriterionResult(criterion_id="c1", verdict=Verdict.PASS, confidence=0.9)
            ],
        )
        result.quality_gate = gate_result.model_dump()

        assert result.quality_gate["passed"] is True
        assert len(result.quality_gate["criteria_results"]) == 1


class TestDefaultJevScorer:
    def test_scorer_uses_decisions_module(self, monkeypatch):
        """DefaultJevScorer routes through the decisions module."""
        from ai_video_editor.decisions.models import Answer

        mock_ask = MagicMock(return_value=Answer(value=True, probability=0.9, backend="rules"))
        monkeypatch.setattr("ai_video_editor.decisions.ask", mock_ask)

        scorer = DefaultJevScorer()
        result = scorer.score_criterion(
            QualityCriterion(id="c1", text="Shows a kill"),
            "Kill at 5s",
            RenderMetadata(duration_seconds=10.0),
        )

        assert result.verdict == Verdict.PASS
        assert mock_ask.called


class TestDefaultVisionClient:
    def test_client_uses_vlm_backend(self, monkeypatch):
        """DefaultVisionClient routes through the VLM backend."""
        mock_backend = MagicMock()
        mock_backend.name = "mock"
        mock_backend.call.return_value = "A kill happens"

        client = DefaultVisionClient(backend=mock_backend)
        description, _usage = client.describe_video(
            [Path("/fake/frame.jpg")],
            system_prompt="sys",
            user_prompt="user",
        )

        assert description == "A kill happens"
        assert mock_backend.call.called


# --- QualityCriterion model tests ---


class TestQualityCriterion:
    def test_model_fields(self):
        criterion = QualityCriterion(id="c1", text="Shows a multikill")
        assert criterion.id == "c1"
        assert criterion.text == "Shows a multikill"
        assert criterion.audio_led is False

    def test_audio_led_flag(self):
        criterion = QualityCriterion(id="c1", text="Has funny voice", audio_led=True)
        assert criterion.audio_led is True

    def test_serialization(self):
        criterion = QualityCriterion(id="c1", text="test", audio_led=True)
        data = criterion.model_dump()
        assert data == {"id": "c1", "text": "test", "audio_led": True}

    def test_backward_compatible_in_edit_plan(self):
        from ai_video_editor.edit_plan import EditPlan

        plan = EditPlan(format="short")
        assert plan.quality_criteria == []

        plan_with_criteria = EditPlan(
            format="short",
            quality_criteria=[
                QualityCriterion(id="c1", text="test"),
            ],
        )
        assert len(plan_with_criteria.quality_criteria) == 1
