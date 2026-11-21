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

    def test_daily_cap_zero_disables_gate(self, test_video, scratch_dir, monkeypatch):
        """GROKBOT_QUALITY_GATE_DAILY_CAP=0 means gate disabled."""
        from ai_video_editor import config

        monkeypatch.setattr(config.settings, "grokbot_quality_gate_enabled", True)
        monkeypatch.setattr(config.settings, "grokbot_quality_gate_daily_cap", 0)

        result, _revisions = run_quality_gate(
            test_video,
            [QualityCriterion(id="c1", text="test")],
            RenderMetadata(),
            scratch_dir,
        )
        assert result.passed
        assert result.skipped
        assert "daily cap" in result.skip_reason.lower() or "disabled" in result.skip_reason.lower()

    def test_daily_cap_negative_disables_gate(self, test_video, scratch_dir, monkeypatch):
        """Negative daily cap means gate disabled."""
        from ai_video_editor import config

        monkeypatch.setattr(config.settings, "grokbot_quality_gate_enabled", True)
        monkeypatch.setattr(config.settings, "grokbot_quality_gate_daily_cap", -1)

        result, _revisions = run_quality_gate(
            test_video,
            [QualityCriterion(id="c1", text="test")],
            RenderMetadata(),
            scratch_dir,
        )
        assert result.passed
        assert result.skipped

    def test_all_criteria_pass(self, test_video, scratch_dir, monkeypatch):
        from ai_video_editor import config

        monkeypatch.setattr(config.settings, "grokbot_quality_gate_enabled", True)
        monkeypatch.setattr(config.settings, "grokbot_quality_gate_daily_cap", 100)

        fake_vision = FakeVisionClient(description="Kill at 5s, good quality")

        # Create a fake batched scorer that returns PASS
        class FakeBatchedScorer:
            def score_all(self, criteria, vision_description, metadata, *, already_passed=None):
                return [
                    CriterionResult(
                        criterion_id=c.id, verdict=Verdict.PASS, confidence=0.9, reason="pass"
                    )
                    for c in criteria
                    if c.id not in (already_passed or set())
                ]

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
                batched_scorer=FakeBatchedScorer(),
            )

        assert result.passed
        assert not result.skipped
        assert len(result.criteria_results) == 1
        assert result.criteria_results[0].verdict == Verdict.PASS
        assert revisions == []

    def test_confident_fail_emits_revision(self, test_video, scratch_dir, monkeypatch):
        from ai_video_editor import config

        monkeypatch.setattr(config.settings, "grokbot_quality_gate_enabled", True)
        monkeypatch.setattr(config.settings, "grokbot_quality_gate_daily_cap", 100)
        monkeypatch.setattr(config.settings, "grokbot_quality_gate_confidence_threshold", 0.7)
        monkeypatch.setattr(config.settings, "grokbot_quality_gate_max_retries", 2)

        fake_vision = FakeVisionClient(description="Scoreboard only, no action")

        class FailingBatchedScorer:
            def score_all(self, criteria, vision_description, metadata, *, already_passed=None):
                return [
                    CriterionResult(
                        criterion_id=c.id,
                        verdict=Verdict.FAIL,
                        confidence=0.85,
                        reason="No kill visible",
                    )
                    for c in criteria
                    if c.id not in (already_passed or set())
                ]

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
                batched_scorer=FailingBatchedScorer(),
            )

        assert not result.passed
        assert len(revisions) == 1
        assert revisions[0].criterion_id == "c1"
        assert revisions[0].retry_number == 1

    def test_low_confidence_fail_does_not_emit_revision(self, test_video, scratch_dir, monkeypatch):
        from ai_video_editor import config

        monkeypatch.setattr(config.settings, "grokbot_quality_gate_enabled", True)
        monkeypatch.setattr(config.settings, "grokbot_quality_gate_daily_cap", 100)
        monkeypatch.setattr(config.settings, "grokbot_quality_gate_confidence_threshold", 0.7)

        fake_vision = FakeVisionClient()

        class LowConfidenceScorer:
            def score_all(self, criteria, vision_description, metadata, *, already_passed=None):
                return [
                    CriterionResult(
                        criterion_id=c.id,
                        verdict=Verdict.FAIL,
                        confidence=0.5,
                        reason="Uncertain",
                    )
                    for c in criteria
                    if c.id not in (already_passed or set())
                ]

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
                batched_scorer=LowConfidenceScorer(),
            )

        assert result.passed
        assert revisions == []

    def test_retry_cap_exhausted(self, test_video, scratch_dir, monkeypatch):
        """When a criterion has exhausted retries, it should not emit a new revision."""
        from ai_video_editor import config

        monkeypatch.setattr(config.settings, "grokbot_quality_gate_enabled", True)
        monkeypatch.setattr(config.settings, "grokbot_quality_gate_daily_cap", 100)
        monkeypatch.setattr(config.settings, "grokbot_quality_gate_confidence_threshold", 0.7)
        monkeypatch.setattr(config.settings, "grokbot_quality_gate_max_retries", 2)

        fake_vision = FakeVisionClient()

        class StillFailingScorer:
            def score_all(self, criteria, vision_description, metadata, *, already_passed=None):
                # Always return FAIL to simulate a criterion that keeps failing
                return [
                    CriterionResult(
                        criterion_id=c.id,
                        verdict=Verdict.FAIL,
                        confidence=0.9,
                        reason="Still failing",
                    )
                    for c in criteria
                ]

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
                batched_scorer=StillFailingScorer(),
            )

        # With max_retries=2 and retry_count=2, we've exhausted retries
        # Result should pass (gate doesn't block), but criterion is marked as exhausted
        assert result.passed
        assert "c1" in result.retries_exhausted
        assert revisions == []


class TestAudioLedCriteria:
    def test_audio_led_uses_metadata_only(self, test_video, scratch_dir, monkeypatch):
        """Audio-led criteria should have empty vision_description in state."""
        from ai_video_editor import config

        monkeypatch.setattr(config.settings, "grokbot_quality_gate_enabled", True)
        monkeypatch.setattr(config.settings, "grokbot_quality_gate_daily_cap", 100)

        fake_vision = FakeVisionClient(description="Boring scoreboard")
        states_recorded = []

        def mock_ask_many(items):
            from ai_video_editor.decisions.models import Answer

            for _q, state in items:
                states_recorded.append(state)
            return [Answer(value=True, probability=0.9, backend="jev") for _ in items]

        monkeypatch.setattr("ai_video_editor.decisions.ask_many", mock_ask_many)

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
            )

        assert len(states_recorded) == 2
        visual_state = states_recorded[0]
        audio_state = states_recorded[1]

        assert visual_state["vision_description"] == "Boring scoreboard"
        assert audio_state["vision_description"] == ""


class TestVisionError:
    def test_vision_error_skips_gate(self, test_video, scratch_dir, monkeypatch):
        from ai_video_editor import config

        monkeypatch.setattr(config.settings, "grokbot_quality_gate_enabled", True)
        monkeypatch.setattr(config.settings, "grokbot_quality_gate_daily_cap", 100)

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
    def test_no_frames_marks_criteria_unsure(self, test_video, scratch_dir, monkeypatch):
        """No frames extracted marks Jev criteria as unsure (not skipped)."""
        from ai_video_editor import config

        monkeypatch.setattr(config.settings, "grokbot_quality_gate_enabled", True)
        monkeypatch.setattr(config.settings, "grokbot_quality_gate_daily_cap", 100)

        with patch("ai_video_editor.grokbot.quality_gate._sample_frames", return_value=[]):
            result, _revisions = run_quality_gate(
                test_video,
                [QualityCriterion(id="c1", text="test")],
                RenderMetadata(duration_seconds=10.0),
                scratch_dir,
            )

        assert result.passed
        assert len(result.criteria_results) == 1
        assert result.criteria_results[0].verdict == Verdict.UNSURE
        assert "no frames" in result.criteria_results[0].reason


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

        mock_ask = MagicMock(return_value=Answer(value=True, probability=0.9, backend="jev"))
        monkeypatch.setattr("ai_video_editor.decisions.ask", mock_ask)

        scorer = DefaultJevScorer()
        result = scorer.score_criterion(
            QualityCriterion(id="c1", text="Shows a kill"),
            "Kill at 5s",
            RenderMetadata(duration_seconds=10.0),
        )

        assert result.verdict == Verdict.PASS
        assert mock_ask.called

    def test_rules_fallback_counts_as_unsure_not_pass(self, monkeypatch):
        """Rules backend fallback must NOT be treated as a confident pass."""
        from ai_video_editor.decisions.models import Answer

        mock_ask = MagicMock(return_value=Answer(value=True, probability=1.0, backend="rules"))
        monkeypatch.setattr("ai_video_editor.decisions.ask", mock_ask)

        scorer = DefaultJevScorer()
        result = scorer.score_criterion(
            QualityCriterion(id="c1", text="Shows a kill"),
            "Kill at 5s",
            RenderMetadata(duration_seconds=10.0),
        )

        assert result.verdict == Verdict.UNSURE
        assert "rules fallback" in result.reason.lower()

    def test_jev_error_counts_as_unsure(self, monkeypatch):
        """When Jev raises an error, the criterion must be unsure, not pass."""
        from ai_video_editor.decisions.models import DecisionError

        mock_ask = MagicMock(side_effect=DecisionError("Jev unavailable"))
        monkeypatch.setattr("ai_video_editor.decisions.ask", mock_ask)

        scorer = DefaultJevScorer()
        result = scorer.score_criterion(
            QualityCriterion(id="c1", text="Shows a kill"),
            "Kill at 5s",
            RenderMetadata(duration_seconds=10.0),
        )

        assert result.verdict == Verdict.UNSURE
        assert result.confidence == 0.0


class TestBatchedJevScoring:
    def test_one_jev_request_per_render(self, test_video, scratch_dir, monkeypatch):
        """All criteria should be scored in one batched Jev request per render."""
        from ai_video_editor import config
        from ai_video_editor.grokbot.quality_gate import BatchedJevScorer

        monkeypatch.setattr(config.settings, "grokbot_quality_gate_enabled", True)
        monkeypatch.setattr(config.settings, "grokbot_quality_gate_daily_cap", 100)

        ask_many_calls = []

        def mock_ask_many(items):
            from ai_video_editor.decisions.models import Answer

            ask_many_calls.append(items)
            return [Answer(value=True, probability=0.9, backend="jev") for _ in items]

        monkeypatch.setattr("ai_video_editor.decisions.ask_many", mock_ask_many)

        scorer = BatchedJevScorer()
        criteria = [
            QualityCriterion(id="c1", text="Shows a kill"),
            QualityCriterion(id="c2", text="Good pacing"),
            QualityCriterion(id="c3", text="Clear audio"),
        ]
        results = scorer.score_all(
            criteria,
            vision_description="A multikill at 5s",
            metadata=RenderMetadata(duration_seconds=30.0),
        )

        assert len(ask_many_calls) == 1
        assert len(ask_many_calls[0]) == 3
        assert len(results) == 3

    def test_retry_only_failed_criteria(self, test_video, scratch_dir, monkeypatch):
        """On retry, only failed/unsure criteria are re-asked; passed stay ticked."""
        from ai_video_editor import config

        monkeypatch.setattr(config.settings, "grokbot_quality_gate_enabled", True)
        monkeypatch.setattr(config.settings, "grokbot_quality_gate_daily_cap", 100)

        fake_vision = FakeVisionClient(description="A multikill at 5s")
        ask_many_calls = []

        def mock_ask_many(items):
            from ai_video_editor.decisions.models import Answer

            ask_many_calls.append(items)
            return [Answer(value=True, probability=0.9, backend="jev") for _ in items]

        monkeypatch.setattr("ai_video_editor.decisions.ask_many", mock_ask_many)

        criteria = [
            QualityCriterion(id="c1", text="Shows a kill"),
            QualityCriterion(id="c2", text="Good pacing"),
            QualityCriterion(id="c3", text="Clear audio"),
        ]

        with patch(
            "ai_video_editor.grokbot.quality_gate._sample_frames",
            return_value=[scratch_dir / "frame_001.jpg"],
        ):
            (scratch_dir / "frame_001.jpg").write_bytes(b"\x00")

            # Simulate retry: c1 already passed, c2 and c3 need re-evaluation
            result, _revisions = run_quality_gate(
                test_video,
                criteria,
                RenderMetadata(duration_seconds=30.0),
                scratch_dir,
                already_passed={"c1"},
                retry_counts={"c1": 1, "c2": 1, "c3": 1},
                vision_client=fake_vision,
            )

        # Only c2 and c3 should be asked (c1 already passed)
        assert len(ask_many_calls) == 1
        assert len(ask_many_calls[0]) == 2

        # Result should have all 3 criteria
        assert len(result.criteria_results) == 3

        # c1 should be PASS with "previously passed" reason
        c1_result = next(cr for cr in result.criteria_results if cr.criterion_id == "c1")
        assert c1_result.verdict == Verdict.PASS
        assert "previously passed" in c1_result.reason

        # c2 and c3 should be from Jev
        c2_result = next(cr for cr in result.criteria_results if cr.criterion_id == "c2")
        c3_result = next(cr for cr in result.criteria_results if cr.criterion_id == "c3")
        assert c2_result.verdict == Verdict.PASS
        assert c3_result.verdict == Verdict.PASS
        assert "Jev" in c2_result.reason
        assert "Jev" in c3_result.reason


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


# --- Timing checks (pure functions, not Jev) ---


class TestTimingChecks:
    def test_check_hook_within_first_n_seconds(self):
        """Hook timing is computed from metadata, not Jev."""
        from ai_video_editor.grokbot.quality_gate import check_timing_criteria

        meta = RenderMetadata(
            duration_seconds=30.0,
            clip_windows=[{"start_s": 0.0, "end_s": 5.0, "has_hook": True}],
        )
        result = check_timing_criteria("hook_within_3s", meta)
        assert result is not None
        assert result.verdict in (Verdict.PASS, Verdict.FAIL)

    def test_check_dead_air_over_n_seconds(self):
        """Dead air uses output timeline gaps, not source trim windows."""
        from ai_video_editor.grokbot.quality_gate import check_timing_criteria

        meta = RenderMetadata(
            duration_seconds=30.0,
            clip_windows=[
                {"start_s": 0.0, "end_s": 5.0, "output_start_s": 0.0, "output_end_s": 5.0},
                {
                    "start_s": 15.0,
                    "end_s": 20.0,
                    "output_start_s": 15.0,
                    "output_end_s": 20.0,
                },  # 10s output gap
            ],
        )
        result = check_timing_criteria("no_dead_air_over_5s", meta)
        assert result is not None
        assert result.verdict == Verdict.FAIL

    def test_dead_air_unsure_without_output_timeline(self):
        from ai_video_editor.grokbot.quality_gate import check_timing_criteria

        meta = RenderMetadata(
            duration_seconds=30.0,
            clip_windows=[
                {"start_s": 0.0, "end_s": 5.0},
                {"start_s": 15.0, "end_s": 20.0},
            ],
        )
        result = check_timing_criteria("no_dead_air_over_5s", meta)
        assert result is not None
        assert result.verdict == Verdict.UNSURE

    def test_hook_unsure_without_has_hook(self):
        from ai_video_editor.grokbot.quality_gate import check_timing_criteria

        meta = RenderMetadata(
            duration_seconds=30.0,
            clip_windows=[{"start_s": 0.0, "end_s": 5.0}],
        )
        result = check_timing_criteria("hook_within_3s", meta)
        assert result is not None
        assert result.verdict == Verdict.UNSURE

    def test_check_duration_limit(self):
        """Duration limit is computed from metadata, not Jev."""
        from ai_video_editor.grokbot.quality_gate import check_timing_criteria

        meta = RenderMetadata(duration_seconds=120.0)
        result = check_timing_criteria("duration_under_60s", meta)
        assert result is not None
        assert result.verdict == Verdict.FAIL

        meta_ok = RenderMetadata(duration_seconds=45.0)
        result_ok = check_timing_criteria("duration_under_60s", meta_ok)
        assert result_ok.verdict == Verdict.PASS

    def test_unknown_timing_criterion_returns_none(self):
        """Non-timing criteria return None (should go to Jev)."""
        from ai_video_editor.grokbot.quality_gate import check_timing_criteria

        meta = RenderMetadata(duration_seconds=30.0)
        result = check_timing_criteria("shows_a_kill", meta)
        assert result is None


class TestCaptionSafeAreaCheck:
    def test_caption_in_safe_area_passes(self):
        """Caption box within safe area passes."""
        from ai_video_editor.grokbot.quality_gate import check_timing_criteria

        # 9:16 portrait: safe area is roughly bottom 15% (y >= 0.85)
        meta = RenderMetadata(
            duration_seconds=30.0,
            aspect_ratio="9:16",
            caption_boxes=[{"x": 0.5, "y": 0.90, "w": 0.8, "h": 0.08}],
        )
        result = check_timing_criteria("caption_safe_area", meta)
        assert result is not None
        assert result.verdict == Verdict.PASS

    def test_caption_outside_safe_area_fails(self):
        """Caption box outside safe area fails."""
        from ai_video_editor.grokbot.quality_gate import check_timing_criteria

        # Caption in upper half of screen - outside safe area
        meta = RenderMetadata(
            duration_seconds=30.0,
            aspect_ratio="9:16",
            caption_boxes=[{"x": 0.5, "y": 0.3, "w": 0.8, "h": 0.08}],
        )
        result = check_timing_criteria("caption_safe_area", meta)
        assert result is not None
        assert result.verdict == Verdict.FAIL

    def test_no_caption_geometry_skips_check(self):
        """Missing caption geometry skips check with UNSURE."""
        from ai_video_editor.grokbot.quality_gate import check_timing_criteria

        meta = RenderMetadata(
            duration_seconds=30.0,
            aspect_ratio="9:16",
            caption_boxes=[],  # No caption geometry
        )
        result = check_timing_criteria("caption_safe_area", meta)
        assert result is not None
        assert result.verdict == Verdict.UNSURE
        assert "no caption geometry" in result.reason

    def test_landscape_safe_area(self):
        """Landscape (16:9) has different safe area."""
        from ai_video_editor.grokbot.quality_gate import check_timing_criteria

        # 16:9 landscape: safe area is typically bottom 20%
        meta = RenderMetadata(
            duration_seconds=30.0,
            aspect_ratio="16:9",
            caption_boxes=[{"x": 0.5, "y": 0.85, "w": 0.6, "h": 0.1}],
        )
        result = check_timing_criteria("caption_safe_area", meta)
        assert result is not None
        assert result.verdict == Verdict.PASS


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

    def test_rejects_hostile_id_and_long_text(self):
        import pytest
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            QualityCriterion(id="Bad-Id", text="ok")
        with pytest.raises(ValidationError):
            QualityCriterion(id="ok_id", text="x" * 301)


class TestPlanHashStability:
    def test_empty_quality_criteria_excluded_from_hash(self):
        from ai_video_editor.edit_plan import EditPlan, plan_hash

        without_field = EditPlan(format="short")
        # Simulate a pre-upgrade dump that never had the field
        legacy = EditPlan.model_validate({"format": "short"})
        assert plan_hash(without_field) == plan_hash(legacy)
        with_empty = EditPlan(format="short", quality_criteria=[])
        assert plan_hash(with_empty) == plan_hash(without_field)
        with_criteria = EditPlan(
            format="short",
            quality_criteria=[QualityCriterion(id="c1", text="hook")],
        )
        assert plan_hash(with_criteria) != plan_hash(without_field)


class TestDailyCapAndRetryState:
    def test_reserve_daily_cap_enforced(self, tmp_path, monkeypatch):
        from ai_video_editor import config
        from ai_video_editor.grokbot import quality_gate as qg

        monkeypatch.setattr(config.settings, "workspace_dir", tmp_path)
        monkeypatch.setattr(config.settings, "grokbot_quality_gate_daily_cap", 2)
        assert qg.reserve_daily_cap() is True
        assert qg.reserve_daily_cap() is True
        assert qg.reserve_daily_cap() is False

    def test_retry_state_roundtrip(self, tmp_path, monkeypatch):
        from ai_video_editor import config
        from ai_video_editor.grokbot.quality_gate import load_retry_state, save_retry_state

        monkeypatch.setattr(config.settings, "workspace_dir", tmp_path)
        save_retry_state("plan-1", retry_counts={"c1": 2}, already_passed={"c2"})
        counts, passed = load_retry_state("plan-1")
        assert counts == {"c1": 2}
        assert passed == {"c2"}


class TestSharedJevState:
    def test_batched_scorer_uses_one_shared_state(self, monkeypatch):
        """All visual criteria share one state so Jev groups into one transport call."""
        from ai_video_editor.decisions.backends.jev import _group_by_state
        from ai_video_editor.decisions.models import Answer
        from ai_video_editor.grokbot.quality_gate import BatchedJevScorer

        ask_many_calls: list = []

        def mock_ask_many(items):
            ask_many_calls.append(items)
            return [Answer(value=True, probability=0.9, backend="jev") for _ in items]

        monkeypatch.setattr("ai_video_editor.decisions.ask_many", mock_ask_many)
        results = BatchedJevScorer().score_all(
            [
                QualityCriterion(id="c1", text="Shows a kill"),
                QualityCriterion(id="c2", text="Good pacing"),
            ],
            vision_description="A multikill",
            metadata=RenderMetadata(duration_seconds=30.0, aspect_ratio="9:16"),
        )
        assert len(ask_many_calls) == 1
        assert len(_group_by_state(ask_many_calls[0])) == 1
        assert "criterion" not in ask_many_calls[0][0][1]
        assert all(r.verdict == Verdict.PASS for r in results)


class TestFlagOffPayload:
    def test_run_quality_gate_helper_returns_none_when_disabled(self, tmp_path, monkeypatch):
        from ai_video_editor import config
        from ai_video_editor.grokbot.box_render import (
            RenderInput,
            RenderOutput,
            RenderResult,
            _run_quality_gate,
        )

        monkeypatch.setattr(config.settings, "grokbot_quality_gate_enabled", False)
        result = RenderResult(
            ok=True,
            edit_plan_id="plan-1",
            edit_plan_version="1",
            asset_id="a1",
            outputs=[
                RenderOutput(aspect="9:16", local_path=tmp_path / "o.mp4", duration_seconds=1.0)
            ],
        )
        (tmp_path / "o.mp4").write_bytes(b"x")
        gate, revs = _run_quality_gate(
            RenderInput(
                edit_plan_id="plan-1",
                edit_plan_version="1",
                asset_id="a1",
                clips=[],
                trims=[{"clip_id": "1", "start_s": 0, "end_s": 1}],
                concat_order=["1"],
                quality_criteria=[{"id": "c1", "text": "ok", "audio_led": False}],
            ),
            result,
            tmp_path,
        )
        assert gate is None
        assert revs == []
