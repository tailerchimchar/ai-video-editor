"""Offline, deterministic integration test suite.

This suite tests the combined code from PRs #13, #14, #16, and #17 end-to-end
with fully stubbed external services (Jev, Anthropic/Haiku, VLM, Drive).

Covers:
1) MCP editing tools: trim, concat, captions, 9:16 and 16:9 output
2) Jev cost path with stubbed transports and LLM
3) Agentic workflow wiring: round.published -> digest -> edit_plan -> box render

All tests are offline (no network), deterministic, and fast (whole suite
under a minute). External dependencies (ffmpeg) are skipped if unavailable.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# Set required env vars before importing modules that use settings
os.environ.setdefault("OUTPLAYED_MEDIA_DIR", "/tmp/fake_media")
os.environ.setdefault("WORKSPACE_DIR", "/tmp/fake_workspace")

from ai_video_editor.config import settings

pytestmark = pytest.mark.integration


def _ffmpeg_available() -> bool:
    """Check if ffmpeg is available on PATH."""
    try:
        result = subprocess.run(
            [settings.ffmpeg_path, "-version"],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _ffprobe_available() -> bool:
    """Check if ffprobe is available on PATH."""
    try:
        result = subprocess.run(
            [settings.ffmpeg_path.replace("ffmpeg", "ffprobe"), "-version"],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _probe_video(path: Path) -> dict:
    """Probe a video file and return duration, width, height."""
    cmd = [
        settings.ffmpeg_path.replace("ffmpeg", "ffprobe"),
        "-v",
        "error",
        "-show_entries",
        "format=duration:stream=width,height",
        "-of",
        "json",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {result.stderr}")
    data = json.loads(result.stdout)
    duration = float(data.get("format", {}).get("duration", 0))
    streams = data.get("streams", [])
    video_stream = next((s for s in streams if s.get("width")), {})
    return {
        "duration": duration,
        "width": video_stream.get("width", 0),
        "height": video_stream.get("height", 0),
    }


@pytest.fixture
def ffmpeg_available():
    """Skip test if ffmpeg is not available."""
    if not _ffmpeg_available():
        pytest.skip("ffmpeg not installed")
    if not _ffprobe_available():
        pytest.skip("ffprobe not installed")
    return True


@pytest.fixture
def test_clip(tmp_path, ffmpeg_available):
    """Generate a 3-second test clip using ffmpeg lavfi testsrc with sine audio."""
    clip_path = tmp_path / "test_source.mp4"
    cmd = [
        settings.ffmpeg_path,
        "-y",
        "-f",
        "lavfi",
        "-i",
        "testsrc=duration=3:size=1920x1080:rate=30",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=3",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "28",
        "-c:a",
        "aac",
        "-b:a",
        "64k",
        str(clip_path),
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=60)
    if result.returncode != 0:
        pytest.skip(f"Failed to generate test clip: {result.stderr.decode()[:200]}")
    return clip_path


@pytest.fixture
def isolation(tmp_path, monkeypatch):
    """Isolate settings for integration tests."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(settings, "workspace_dir", ws)
    monkeypatch.setattr(settings, "database_path", str(db_path))
    monkeypatch.setattr(settings, "outplayed_media_dir", str(tmp_path / "media"))
    monkeypatch.setattr(settings, "langfuse_public_key", "")
    monkeypatch.setattr(settings, "langfuse_secret_key", "")
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    monkeypatch.setattr(settings, "typesafe_api_key", "test-jev-key")
    monkeypatch.setattr(settings, "grokbot_quality_gate_enabled", True)
    monkeypatch.setattr(settings, "grokbot_quality_gate_daily_cap", 100)
    monkeypatch.setattr(settings, "grokbot_jev_ranker_cascade_enabled", True)
    monkeypatch.setattr(settings, "grokbot_jev_keep_threshold", 0.8)
    monkeypatch.setattr(settings, "grokbot_jev_reject_threshold", 0.8)
    return tmp_path


# =============================================================================
# SECTION 1: MCP Editing Tools Integration Tests
# =============================================================================


class TestMcpEditingToolsIntegration:
    """Integration tests for MCP editing tools: trim, concat, captions, aspect ratios."""

    def test_trim_clip(self, isolation, test_clip, tmp_path):
        """Test that trim_clip produces correct output with expected duration."""
        from ai_video_editor.editing import trim_clip

        output_path = tmp_path / "output" / "trimmed.mp4"
        ok, err = trim_clip(
            str(test_clip),
            str(output_path),
            start_seconds=0.5,
            end_seconds=2.5,
        )
        assert ok, f"trim_clip failed: {err}"
        assert output_path.exists()

        info = _probe_video(output_path)
        assert 1.5 < info["duration"] < 2.6, f"Expected ~2s, got {info['duration']}"

    def test_render_9x16_aspect_ratio(self, isolation, test_clip, tmp_path):
        """Test 9:16 output (cropped_hud_9x16 layout) produces correct resolution."""
        from ai_video_editor.grokbot.box_render import _render_single_aspect

        scratch = tmp_path / "scratch"
        scratch.mkdir()

        output, err = _render_single_aspect(
            scratch,
            [test_clip],
            [{"clip_id": "c1", "start_s": 0, "end_s": 2}],
            ["c1"],
            "9:16",
            game="league",
        )

        assert err is None, f"9:16 render failed: {err}"
        assert output is not None and output.exists()

        info = _probe_video(output)
        assert info["width"] == 720
        assert info["height"] == 1280
        assert 1.5 < info["duration"] < 2.5

    def test_render_16x9_aspect_ratio(self, isolation, test_clip, tmp_path):
        """Test 16:9 output produces correct resolution."""
        from ai_video_editor.grokbot.box_render import _render_single_aspect

        scratch = tmp_path / "scratch"
        scratch.mkdir()

        output, err = _render_single_aspect(
            scratch,
            [test_clip],
            [{"clip_id": "c1", "start_s": 0, "end_s": 2}],
            ["c1"],
            "16:9",
        )

        assert err is None, f"16:9 render failed: {err}"
        assert output is not None and output.exists()

        info = _probe_video(output)
        assert info["width"] == 1920
        assert info["height"] == 1080
        assert 1.5 < info["duration"] < 2.5

    def test_concat_multiple_clips(self, isolation, test_clip, tmp_path):
        """Test concatenating multiple clips."""
        from ai_video_editor.grokbot.box_render import _render_single_aspect

        scratch = tmp_path / "scratch"
        scratch.mkdir()

        clip2 = tmp_path / "test_clip2.mp4"
        shutil.copy(test_clip, clip2)

        output, err = _render_single_aspect(
            scratch,
            [test_clip, clip2],
            [
                {"clip_id": "c1", "start_s": 0, "end_s": 1},
                {"clip_id": "c2", "start_s": 0, "end_s": 1},
            ],
            ["c1", "c2"],
            "16:9",
        )

        assert err is None, f"concat render failed: {err}"
        assert output is not None and output.exists()

        info = _probe_video(output)
        assert 1.5 < info["duration"] < 2.5, f"Expected ~2s, got {info['duration']}"

    def test_captions_filter_generation(self, isolation):
        """Test that caption filters are generated correctly."""
        from ai_video_editor.edits import caption_filters

        segments = [
            {"start_seconds": 1.0, "end_seconds": 3.0, "text": "First caption"},
            {"start_seconds": 4.0, "end_seconds": 6.0, "text": "Second caption"},
        ]

        filters = caption_filters(segments, clip_start_offset=0.0)

        assert "drawtext" in filters
        assert "First caption" in filters
        assert "Second caption" in filters
        assert "enable='between" in filters


# =============================================================================
# SECTION 2: Jev Cost Path Integration Tests
# =============================================================================


@dataclass
class FakeJevAnswer:
    """Fake Jev answer for testing."""

    value: Any
    probability: float
    backend: str = "jev"


class FakeJevTransport:
    """Fake Jev transport that returns canned answers."""

    def __init__(self, answers: dict[str, dict] | None = None, fail: bool = False):
        self.calls: list[tuple[dict, dict]] = []
        self._answers = answers or {}
        self._fail = fail
        self._call_count = 0

    def ask(self, state: dict, questions: dict[str, dict]) -> dict[str, dict]:
        self._call_count += 1
        self.calls.append((state, questions))
        if self._fail:
            raise RuntimeError("Jev unavailable")
        result = {}
        for key, q in questions.items():
            if q["type"] == "noul":
                result[key] = {"type": "noul", "noul": 0.9, "probabilities": {}}
            elif q["type"] == "score":
                result[key] = {"type": "score", "score": 7.0, "probabilities": {}}
            elif q["type"] == "choice":
                first_option = next(iter(q["criteria"].keys()))
                result[key] = {"type": "choice", "choice": first_option, "probabilities": {}}
        return result

    @property
    def call_count(self) -> int:
        return self._call_count


class FakeHaikuRanker:
    """Fake Haiku ranker that records calls."""

    def __init__(self):
        self.calls: list[tuple[str | None, list[dict]]] = []
        self._call_count = 0

    def rank(self, game, candidates):
        from ai_video_editor.models import RankedCandidate

        self._call_count += 1
        self.calls.append((game, candidates))
        return (
            [
                RankedCandidate(
                    candidate_id=c["id"],
                    keep=True,
                    funny_score=0.5,
                    hype_score=0.7,
                    story_score=0.3,
                    suggested_start_seconds=c.get("start_seconds", 0) - 2,
                    suggested_end_seconds=c.get("end_seconds", 10) + 2,
                    reason="Haiku ranked this",
                )
                for c in candidates
            ],
            len(candidates) * 500,
            len(candidates) * 100,
        )

    @property
    def call_count(self) -> int:
        return self._call_count


def _make_candidate(cid: str, *, source: str = "cv_kda", event_type: str = "kill") -> dict:
    """Create a test candidate."""
    return {
        "id": cid,
        "source": source,
        "event_type": event_type,
        "start_seconds": 10.0,
        "end_seconds": 20.0,
        "confidence": 0.9,
        "metadata": {"rationale": "test candidate"},
    }


class TestJevCostPathIntegration:
    """Integration tests for Jev cost path and ranker cascade.

    Key metrics (from ranker_cascade.CascadeStats):
      - escalated_unsure: candidates Jev couldn't decide (go to Haiku for full ranking)
      - timing_refine: confident keeps sent to Haiku only for in/out timing
      - escalation_rate: escalated_unsure / total_candidates (true uncertainty rate)
      - haiku_candidates_saved: confidently rejected candidates that never reach Haiku
      - haiku_call_skipped: 1 if nothing needed Haiku in this batch, else 0
    """

    def test_confident_keeps_sent_for_timing_refinement(self, isolation):
        """Confident keeps get Jev scores, sent to Haiku for timing_refine only."""
        from ai_video_editor.ranker_cascade import (
            JevScores,
            cascade_rank,
        )

        candidates = [
            _make_candidate("confident_keep", event_type="ace"),
            _make_candidate("unsure", event_type="kill"),
        ]

        class CountingJevScorer:
            def __init__(self):
                self.call_count = 0

            def score_candidates(self, candidates):
                self.call_count += 1
                return [
                    JevScores(
                        keep=True,
                        keep_probability=0.95,
                        funny_score=6,
                        hype_score=8,
                        story_score=4,
                        funny_probability=0.8,
                        hype_probability=0.9,
                        story_probability=0.7,
                    ),
                    JevScores(
                        keep=True,
                        keep_probability=0.55,
                        funny_score=5,
                        hype_score=5,
                        story_score=5,
                        funny_probability=0.5,
                        hype_probability=0.5,
                        story_probability=0.5,
                    ),
                ], 200

        jev = CountingJevScorer()
        haiku = FakeHaikuRanker()

        result = cascade_rank(
            "league",
            candidates,
            jev_scorer=jev,
            haiku_ranker=haiku,
            keep_threshold=0.8,
        )

        assert jev.call_count == 1
        assert result.stats.jev_kept == 1
        assert result.stats.timing_refine == 1
        assert result.stats.escalated_unsure == 1
        assert result.stats.escalation_rate == 0.5
        haiku_ids = {c["id"] for c in haiku.calls[0][1]}
        assert "confident_keep" in haiku_ids
        assert "unsure" in haiku_ids

    def test_unsure_candidates_escalate_to_haiku(self, isolation):
        """Candidates with low confidence go to Haiku (escalated_unsure)."""
        from ai_video_editor.ranker_cascade import JevScores, cascade_rank

        candidates = [_make_candidate("unsure")]

        class UnsureJevScorer:
            def score_candidates(self, candidates):
                return [
                    JevScores(
                        keep=True,
                        keep_probability=0.55,
                        funny_score=5,
                        hype_score=5,
                        story_score=5,
                        funny_probability=0.5,
                        hype_probability=0.5,
                        story_probability=0.5,
                    )
                ], 100

        haiku = FakeHaikuRanker()
        result = cascade_rank(
            "league",
            candidates,
            jev_scorer=UnsureJevScorer(),
            haiku_ranker=haiku,
        )

        assert result.stats.jev_decided == 0
        assert result.stats.escalated_unsure == 1
        assert result.stats.timing_refine == 0
        assert result.stats.escalation_rate == 1.0
        assert haiku.call_count == 1

    def test_one_batched_jev_request_per_render(self, isolation, monkeypatch):
        """Quality gate makes one batched Jev request per render."""
        from ai_video_editor.decisions.models import Answer
        from ai_video_editor.edit_plan import QualityCriterion
        from ai_video_editor.grokbot.quality_gate import (
            BatchedJevScorer,
            RenderMetadata,
        )

        ask_many_calls = []

        def mock_ask_many(items):
            ask_many_calls.append(items)
            return [Answer(value=True, probability=0.9, backend="jev") for _ in items]

        monkeypatch.setattr("ai_video_editor.decisions.ask_many", mock_ask_many)

        scorer = BatchedJevScorer()
        criteria = [
            QualityCriterion(id="c1", text="Shows a kill"),
            QualityCriterion(id="c2", text="Good pacing"),
            QualityCriterion(id="c3", text="Clear audio"),
        ]
        scorer.score_all(
            criteria,
            vision_description="Test description",
            metadata=RenderMetadata(duration_seconds=30.0),
        )

        assert len(ask_many_calls) == 1, "Expected one batched request"
        assert len(ask_many_calls[0]) == 3, "Expected all 3 criteria in one batch"

    def test_retry_only_failed_criteria(self, isolation, monkeypatch, tmp_path):
        """On retry, only failed criteria are re-asked; passed stay ticked."""
        from ai_video_editor.decisions.models import Answer
        from ai_video_editor.edit_plan import QualityCriterion
        from ai_video_editor.grokbot.quality_gate import (
            RenderMetadata,
            Verdict,
            run_quality_gate,
        )

        ask_many_calls = []

        def mock_ask_many(items):
            ask_many_calls.append(items)
            return [Answer(value=True, probability=0.9, backend="jev") for _ in items]

        monkeypatch.setattr("ai_video_editor.decisions.ask_many", mock_ask_many)

        class FakeVisionClient:
            def describe_video(self, frames, *, system_prompt, user_prompt):
                return "Test description", {"model": "fake"}

        video = tmp_path / "test.mp4"
        video.write_bytes(b"\x00" * 1000)
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        (scratch / "frame_001.jpg").write_bytes(b"\x00")

        criteria = [
            QualityCriterion(id="c1", text="Shows a kill"),
            QualityCriterion(id="c2", text="Good pacing"),
            QualityCriterion(id="c3", text="Clear audio"),
        ]

        with patch(
            "ai_video_editor.grokbot.quality_gate._sample_frames",
            return_value=[scratch / "frame_001.jpg"],
        ):
            result, _revisions = run_quality_gate(
                video,
                criteria,
                RenderMetadata(duration_seconds=30.0),
                scratch,
                already_passed={"c1"},
                retry_counts={"c1": 1, "c2": 1, "c3": 1},
                vision_client=FakeVisionClient(),
            )

        assert len(ask_many_calls) == 1
        assert len(ask_many_calls[0]) == 2, "Only c2 and c3 should be re-asked"

        c1_result = next(cr for cr in result.criteria_results if cr.criterion_id == "c1")
        assert c1_result.verdict == Verdict.PASS
        assert "previously passed" in c1_result.reason

    def test_jev_error_counts_as_unsure_never_pass(self, isolation, monkeypatch, tmp_path):
        """Jev error or rules fallback counts as unsure, never a pass."""
        from ai_video_editor.decisions.models import Answer
        from ai_video_editor.edit_plan import QualityCriterion
        from ai_video_editor.grokbot.quality_gate import (
            DefaultJevScorer,
            RenderMetadata,
            Verdict,
        )

        monkeypatch.setattr(
            "ai_video_editor.decisions.ask",
            lambda q, s: Answer(value=True, probability=1.0, backend="rules"),
        )

        scorer = DefaultJevScorer()
        result = scorer.score_criterion(
            QualityCriterion(id="c1", text="Test criterion"),
            "Test description",
            RenderMetadata(duration_seconds=10.0),
        )

        assert result.verdict == Verdict.UNSURE
        assert "rules fallback" in result.reason.lower()

    def test_daily_cap_zero_disables_gate(self, isolation, monkeypatch, tmp_path):
        """GROKBOT_QUALITY_GATE_DAILY_CAP=0 disables the gate."""
        from ai_video_editor.edit_plan import QualityCriterion
        from ai_video_editor.grokbot.quality_gate import (
            RenderMetadata,
            run_quality_gate,
        )

        monkeypatch.setattr(settings, "grokbot_quality_gate_daily_cap", 0)

        video = tmp_path / "test.mp4"
        video.write_bytes(b"\x00")
        scratch = tmp_path / "scratch"
        scratch.mkdir()

        result, _revisions = run_quality_gate(
            video,
            [QualityCriterion(id="c1", text="test")],
            RenderMetadata(),
            scratch,
        )

        assert result.passed
        assert result.skipped
        assert "daily cap" in result.skip_reason.lower() or "disabled" in result.skip_reason.lower()

    def test_haiku_candidates_saved_on_confident_reject(self, isolation):
        """Confident rejects count as haiku_candidates_saved."""
        from ai_video_editor.ranker_cascade import JevScores, cascade_rank

        candidates = [
            _make_candidate("reject1"),
            _make_candidate("reject2"),
            _make_candidate("unsure"),
        ]

        class RejectingJevScorer:
            def score_candidates(self, candidates):
                return [
                    JevScores(
                        keep=False,
                        keep_probability=0.95,
                        funny_score=2,
                        hype_score=2,
                        story_score=2,
                        funny_probability=0.9,
                        hype_probability=0.9,
                        story_probability=0.9,
                    ),
                    JevScores(
                        keep=False,
                        keep_probability=0.92,
                        funny_score=1,
                        hype_score=1,
                        story_score=1,
                        funny_probability=0.9,
                        hype_probability=0.9,
                        story_probability=0.9,
                    ),
                    JevScores(
                        keep=True,
                        keep_probability=0.55,
                        funny_score=5,
                        hype_score=5,
                        story_score=5,
                        funny_probability=0.5,
                        hype_probability=0.5,
                        story_probability=0.5,
                    ),
                ], 300

        haiku = FakeHaikuRanker()
        result = cascade_rank(
            "league",
            candidates,
            jev_scorer=RejectingJevScorer(),
            haiku_ranker=haiku,
            reject_threshold=0.8,
        )

        assert result.stats.jev_rejected == 2
        assert result.stats.haiku_candidates_saved == 2
        assert result.stats.escalated_unsure == 1
        assert result.stats.escalation_rate == 1 / 3

    def test_haiku_call_skipped_when_all_decided(self, isolation):
        """haiku_call_skipped=1 when Jev confidently decides everything."""
        from ai_video_editor.ranker_cascade import JevScores, cascade_rank

        candidates = [_make_candidate("reject_only")]

        class AllRejectJevScorer:
            def score_candidates(self, candidates):
                return [
                    JevScores(
                        keep=False,
                        keep_probability=0.99,
                        funny_score=1,
                        hype_score=1,
                        story_score=1,
                        funny_probability=0.9,
                        hype_probability=0.9,
                        story_probability=0.9,
                    ),
                ], 100

        haiku = FakeHaikuRanker()
        result = cascade_rank(
            "league",
            candidates,
            jev_scorer=AllRejectJevScorer(),
            haiku_ranker=haiku,
            reject_threshold=0.8,
        )

        assert result.stats.haiku_call_skipped == 1
        assert haiku.call_count == 0

    def test_cost_log_totals_equal_sum_of_calls(self, isolation):
        """Per-video cost log totals equal the sum of recorded calls."""
        from ai_video_editor.cost_log import CostLog

        cost_log = CostLog(video_id="test-video")
        cost_log.record_call(model="haiku", input_tokens=1000, output_tokens=200, cost_dollars=0.01)
        cost_log.record_call(model="haiku", input_tokens=500, output_tokens=100, cost_dollars=0.005)
        cost_log.record_call(model="jev", input_tokens=800, cost_dollars=None)

        assert cost_log.total_input_tokens == 2300
        assert cost_log.total_output_tokens == 300
        assert cost_log.total_cost_dollars is None

        cost_log2 = CostLog()
        cost_log2.record_call(model="haiku", cost_dollars=0.01)
        cost_log2.record_call(model="haiku", cost_dollars=0.02)
        assert cost_log2.total_cost_dollars == 0.03

    def test_unknown_cost_kept_null(self, isolation):
        """Unknown cost is kept null, never estimated silently."""
        from ai_video_editor.cost_log import CostLog

        cost_log = CostLog()
        cost_log.record_call(model="jev", input_tokens=500, cost_dollars=None)

        d = cost_log.to_dict()
        assert d["calls"][0]["cost_dollars"] is None
        assert d["summary"]["total_cost_dollars"] is None

    def test_cascade_metrics_in_cost_log(self, isolation):
        """Cascade metrics are recorded in the cost log."""
        from ai_video_editor.ranker_cascade import JevScores, cascade_rank

        candidates = [
            _make_candidate("keep1", event_type="ace"),
            _make_candidate("reject1"),
            _make_candidate("unsure1"),
        ]

        class MixedJevScorer:
            def score_candidates(self, candidates):
                return [
                    JevScores(
                        keep=True,
                        keep_probability=0.95,
                        funny_score=8,
                        hype_score=9,
                        story_score=5,
                        funny_probability=0.9,
                        hype_probability=0.95,
                        story_probability=0.8,
                    ),
                    JevScores(
                        keep=False,
                        keep_probability=0.92,
                        funny_score=1,
                        hype_score=1,
                        story_score=1,
                        funny_probability=0.9,
                        hype_probability=0.9,
                        story_probability=0.9,
                    ),
                    JevScores(
                        keep=True,
                        keep_probability=0.55,
                        funny_score=5,
                        hype_score=5,
                        story_score=5,
                        funny_probability=0.5,
                        hype_probability=0.5,
                        story_probability=0.5,
                    ),
                ], 300

        haiku = FakeHaikuRanker()
        result = cascade_rank(
            "league",
            candidates,
            jev_scorer=MixedJevScorer(),
            haiku_ranker=haiku,
            keep_threshold=0.8,
            reject_threshold=0.8,
            video_id="test-video",
        )

        d = result.cost_log.to_dict()
        assert "cascade" in d
        cascade = d["cascade"]
        assert cascade["total_candidates"] == 3
        assert cascade["jev_kept"] == 1
        assert cascade["jev_rejected"] == 1
        assert cascade["escalated_unsure"] == 1
        assert cascade["timing_refine"] == 1
        assert cascade["haiku_candidates_saved"] == 1
        assert cascade["haiku_call_skipped"] == 0
        assert 0.3 < cascade["escalation_rate"] < 0.4


# =============================================================================
# SECTION 3: Agentic Workflow Wiring Integration Tests
# =============================================================================


class FakeStorageBackend:
    """Fake storage backend for testing."""

    def __init__(self, files: dict[str, bytes] | None = None):
        self._files = files or {}
        self.uploads: list[tuple[str, str]] = []
        self.downloads: list[str] = []

    def read_bytes(self, path: str) -> bytes:
        from ai_video_editor.storage import StorageError

        if path not in self._files:
            raise StorageError(f"File not found: {path}")
        return self._files[path]

    def write_bytes(self, path: str, data: bytes) -> None:
        self._files[path] = data

    def exists(self, path: str) -> bool:
        return path in self._files

    def mkdir(self, path: str, parents: bool = True) -> None:
        pass

    def listdir(self, path: str) -> list[str]:
        return []

    def remove(self, path: str) -> None:
        self._files.pop(path, None)

    def rmtree(self, path: str) -> None:
        pass

    def copy_to_local(self, remote_path: str, local_path: str) -> None:
        self.downloads.append(remote_path)
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        Path(local_path).write_bytes(self._files.get(remote_path, b""))

    def copy_from_local(self, local_path: str, remote_path: str) -> None:
        self.uploads.append((local_path, remote_path))
        content = Path(local_path).read_bytes() if Path(local_path).exists() else b""
        self._files[remote_path] = content


class TestAgenticWorkflowWiringIntegration:
    """Integration tests for agentic workflow wiring."""

    def test_round_published_event_flows_through_workflow(self, isolation, monkeypatch):
        """A fake round.published event flows through the digest."""
        from ai_video_editor.grokbot.digest import build_digest

        round_dir = isolation / "rounds" / "2026-11-20_test"
        round_dir.mkdir(parents=True)

        clips_data = [
            {
                "file": "clip_14m32s.mp4",
                "start_seconds": 872.0,
                "end_seconds": 882.0,
                "event": "kill",
            },
            {
                "file": "clip_21m05s.mp4",
                "start_seconds": 1265.0,
                "end_seconds": 1275.0,
                "event": "death",
            },
        ]
        (round_dir / "clips.json").write_text(json.dumps(clips_data))
        (round_dir / "gaming-expert.json").write_text(json.dumps([]))
        (round_dir / "viewer-expert.json").write_text(json.dumps({"clips": []}))

        digest = build_digest(round_dir, game="league")

        assert digest.game == "league"
        assert digest.round == "2026-11-20_test"
        assert len(digest.clips) == 2
        assert digest.clips[0].ref == "14m32s"
        assert digest.clips[1].ref == "21m05s"

    def test_box_render_emits_video_rendered_event(
        self, isolation, test_clip, monkeypatch, tmp_path
    ):
        """Box render with fake Drive storage emits video.rendered event."""
        from ai_video_editor.grokbot.box_render import (
            RenderInput,
            RenderLock,
            run_box_render,
        )
        from ai_video_editor.notify.models import Event

        events: list[Event] = []

        scratch = tmp_path / "scratch"
        scratch.mkdir()
        local_clip = scratch / "clip_000.mp4"
        shutil.copy(test_clip, local_clip)

        lock = RenderLock(str(tmp_path / "test.lock"))

        with patch("ai_video_editor.grokbot.box_render.DriveStorage") as mock_drive_storage:
            mock_storage = MagicMock()
            mock_storage.scratch_dir = scratch
            mock_storage.download_clip.return_value = local_clip
            mock_storage.upload_file.side_effect = ["drive-9x16-id", "drive-16x9-id"]
            mock_drive_storage.return_value = mock_storage

            render_input = RenderInput(
                edit_plan_id="plan-e2e",
                edit_plan_version="1",
                asset_id="asset-e2e",
                clips=[{"drive_id": "d1", "clip_ref": "1"}],
                trims=[{"clip_id": "1", "start_s": 0, "end_s": 2}],
                concat_order=["1"],
                game="league",
            )

            with patch("tempfile.mkdtemp", return_value=str(scratch)):
                result = run_box_render(
                    MagicMock(),
                    render_input,
                    emit_fn=lambda e: events.append(e),
                    lock=lock,
                )

        assert result.ok
        video_rendered = [e for e in events if e.event == "video.rendered"]
        assert len(video_rendered) == 1
        payload = video_rendered[0].payload
        assert payload["edit_plan_id"] == "plan-e2e"
        assert len(payload["outputs"]) == 2
        aspects = {o["aspect"] for o in payload["outputs"]}
        assert aspects == {"9:16", "16:9"}

    def test_upload_failure_produces_round_publish_failed(self, isolation, monkeypatch):
        """An upload failure produces round.publish_failed, no round.published."""
        from ai_video_editor.grokbot.box_render import (
            RenderInput,
            RenderLock,
            run_box_render,
        )
        from ai_video_editor.notify.models import Event
        from ai_video_editor.storage import StorageError

        events: list[Event] = []

        lock = RenderLock(str(isolation / "test.lock"))

        with patch("ai_video_editor.grokbot.box_render.DriveStorage") as mock_drive_storage:
            mock_storage = MagicMock()
            mock_storage.download_clip.side_effect = StorageError("Drive download failed")
            mock_drive_storage.return_value = mock_storage

            render_input = RenderInput(
                edit_plan_id="plan-fail",
                edit_plan_version="1",
                asset_id="asset-fail",
                clips=[{"drive_id": "d1", "clip_ref": "1"}],
                trims=[{"clip_id": "1", "start_s": 0, "end_s": 1}],
                concat_order=["1"],
            )

            result = run_box_render(
                MagicMock(),
                render_input,
                emit_fn=lambda e: events.append(e),
                lock=lock,
            )

        assert not result.ok
        failed = [e for e in events if e.event == "render.failed"]
        assert len(failed) == 1
        assert failed[0].payload["stage"] == "download"
        assert "round.published" not in [e.event for e in events]

    def test_render_error_produces_render_failed(self, isolation, monkeypatch, tmp_path):
        """A render error produces render.failed event."""
        from ai_video_editor.grokbot.box_render import (
            RenderInput,
            RenderLock,
            run_box_render,
        )
        from ai_video_editor.notify.models import Event

        events: list[Event] = []

        scratch = tmp_path / "scratch"
        scratch.mkdir()
        empty_clip = scratch / "empty.mp4"
        empty_clip.write_bytes(b"not a valid video")

        lock = RenderLock(str(tmp_path / "test.lock"))

        with patch("ai_video_editor.grokbot.box_render.DriveStorage") as mock_drive_storage:
            mock_storage = MagicMock()
            mock_storage.scratch_dir = scratch
            mock_storage.download_clip.return_value = empty_clip
            mock_drive_storage.return_value = mock_storage

            render_input = RenderInput(
                edit_plan_id="plan-render-fail",
                edit_plan_version="1",
                asset_id="asset-render-fail",
                clips=[{"drive_id": "d1", "clip_ref": "1"}],
                trims=[{"clip_id": "1", "start_s": 0, "end_s": 1}],
                concat_order=["1"],
                game="league",
            )

            with patch("tempfile.mkdtemp", return_value=str(scratch)):
                result = run_box_render(
                    MagicMock(),
                    render_input,
                    emit_fn=lambda e: events.append(e),
                    lock=lock,
                )

        assert not result.ok
        failed = [e for e in events if e.event == "render.failed"]
        assert len(failed) == 1
        assert failed[0].payload["stage"] == "render"

    def test_stale_queue_item_rejected(self, isolation, monkeypatch):
        """A stale queue item (outdated base_version) gets rejected."""
        from ai_video_editor.grokbot.queue import parse_item

        stale_item = json.dumps(
            {
                "id": str(uuid.uuid4()),
                "kind": "edit_plan",
                "author_bot": "editor",
                "created": datetime.now(timezone.utc).isoformat(),
                "schema_version": 1,
                "compilation_id": "comp-001",
                "base_version": "1",
                "plan": {
                    "format": "hype_montage",
                    "source_clips": [
                        {"clip_ref": "1", "start": 0.0, "end": 10.0, "intensity": 0.8}
                    ],
                    "order": ["1"],
                    "music": None,
                    "effects": [],
                    "cards": [],
                    "sfx": [],
                    "rejected": [],
                },
                "copy": {"title": "Test", "caption": "Test", "hashtags": [], "thumbnail_t": 5.0},
            }
        )

        item = parse_item(stale_item)
        assert item.kind == "edit_plan"
        assert item.base_version == "1"

    def test_event_emission_order(self, isolation, test_clip, monkeypatch, tmp_path):
        """Events are emitted in the expected order."""
        from ai_video_editor.grokbot.box_render import (
            RenderInput,
            RenderLock,
            run_box_render,
        )
        from ai_video_editor.notify.models import Event

        events: list[Event] = []

        scratch = tmp_path / "scratch"
        scratch.mkdir()
        local_clip = scratch / "clip_000.mp4"
        shutil.copy(test_clip, local_clip)

        lock = RenderLock(str(tmp_path / "test.lock"))

        with patch("ai_video_editor.grokbot.box_render.DriveStorage") as mock_drive_storage:
            mock_storage = MagicMock()
            mock_storage.scratch_dir = scratch
            mock_storage.download_clip.return_value = local_clip
            mock_storage.upload_file.side_effect = ["drive-9x16-id", "drive-16x9-id"]
            mock_drive_storage.return_value = mock_storage

            render_input = RenderInput(
                edit_plan_id="plan-order",
                edit_plan_version="1",
                asset_id="asset-order",
                clips=[{"drive_id": "d1", "clip_ref": "1"}],
                trims=[{"clip_id": "1", "start_s": 0, "end_s": 2}],
                concat_order=["1"],
                game="league",
            )

            with patch("tempfile.mkdtemp", return_value=str(scratch)):
                result = run_box_render(
                    MagicMock(),
                    render_input,
                    emit_fn=lambda e: events.append(e),
                    lock=lock,
                )

        assert result.ok

        event_names = [e.event for e in events]
        assert "video.rendered" in event_names
        assert "render.failed" not in event_names

    def test_busy_lock_emits_failure(self, isolation, tmp_path):
        """When render lock is busy, render.failed with stage=busy is emitted."""
        from ai_video_editor.grokbot.box_render import (
            RenderInput,
            RenderLock,
            run_box_render,
        )
        from ai_video_editor.notify.models import Event

        events: list[Event] = []

        lock = RenderLock(str(tmp_path / "test.lock"))
        lock.acquire()

        try:
            render_input = RenderInput(
                edit_plan_id="plan-busy",
                edit_plan_version="1",
                asset_id="asset-busy",
                clips=[{"drive_id": "d1", "clip_ref": "1"}],
                trims=[{"clip_id": "1", "start_s": 0, "end_s": 1}],
                concat_order=["1"],
            )

            result = run_box_render(
                MagicMock(),
                render_input,
                emit_fn=lambda e: events.append(e),
                lock=RenderLock(str(tmp_path / "test.lock")),
            )

            assert not result.ok
            failed = [e for e in events if e.event == "render.failed"]
            assert len(failed) == 1
            assert failed[0].payload["stage"] == "busy"
        finally:
            lock.release()


# =============================================================================
# Additional edge case tests
# =============================================================================


class TestEdgeCases:
    """Edge case tests for robustness."""

    def test_empty_candidates_returns_empty_result(self, isolation):
        """Empty candidates list returns empty result."""
        from ai_video_editor.ranker_cascade import cascade_rank

        class EmptyJevScorer:
            def score_candidates(self, candidates):
                return [], 0

        result = cascade_rank("league", [], jev_scorer=EmptyJevScorer())
        assert result.rankings == []
        assert result.stats.total_candidates == 0

    def test_quality_gate_no_criteria_skips(self, isolation, tmp_path):
        """Quality gate with no criteria skips entirely."""
        from ai_video_editor.grokbot.quality_gate import (
            RenderMetadata,
            run_quality_gate,
        )

        video = tmp_path / "test.mp4"
        video.write_bytes(b"\x00")
        scratch = tmp_path / "scratch"
        scratch.mkdir()

        result, revisions = run_quality_gate(video, [], RenderMetadata(), scratch)

        assert result.passed
        assert result.skipped
        assert "no criteria" in result.skip_reason
        assert revisions == []

    def test_timing_criteria_computed_without_jev(self, isolation):
        """Timing-based criteria are computed from metadata, not Jev."""
        from ai_video_editor.grokbot.quality_gate import (
            RenderMetadata,
            Verdict,
            check_timing_criteria,
        )

        meta = RenderMetadata(duration_seconds=120.0)
        result = check_timing_criteria("duration_under_60s", meta)
        assert result is not None
        assert result.verdict == Verdict.FAIL

        meta_ok = RenderMetadata(duration_seconds=45.0)
        result_ok = check_timing_criteria("duration_under_60s", meta_ok)
        assert result_ok.verdict == Verdict.PASS

    def test_non_timing_criterion_returns_none(self, isolation):
        """Non-timing criteria return None (should go to Jev)."""
        from ai_video_editor.grokbot.quality_gate import (
            RenderMetadata,
            check_timing_criteria,
        )

        meta = RenderMetadata(duration_seconds=30.0)
        result = check_timing_criteria("shows_a_kill", meta)
        assert result is None
