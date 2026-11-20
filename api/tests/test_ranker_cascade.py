"""Tests for the Jev ranker cascade.

Covers:
  - Confidence routing: confident keeps, confident rejects, unsure to Haiku
  - Fail-safe: Jev errors or rules fallback -> unsure
  - Threshold behavior
  - Privacy boundary: box path never includes transcript fields
  - Integration: only unsure candidates reach Haiku
  - Cost logging
"""

from __future__ import annotations

import pytest

from ai_video_editor.config import settings
from ai_video_editor.cost_log import CostLog
from ai_video_editor.decisions import Answer
from ai_video_editor.models import RankedCandidate
from ai_video_editor.ranker_cascade import (
    CascadeMode,
    DefaultJevScorer,
    JevScores,
    build_candidate_state_box,
    build_candidate_state_pc,
    cascade_rank,
    rank_candidates_with_cascade,
    state_contains_transcript,
)


@pytest.fixture(autouse=True)
def _isolate_settings(monkeypatch, tmp_path):
    """Isolate settings for ranker cascade tests."""
    monkeypatch.setattr(settings, "workspace_dir", tmp_path / "ws")
    monkeypatch.setattr(settings, "database_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "grokbot_jev_ranker_cascade_enabled", False)
    monkeypatch.setattr(settings, "grokbot_jev_keep_threshold", 0.8)
    monkeypatch.setattr(settings, "grokbot_jev_reject_threshold", 0.8)


def _candidate(
    cid: str,
    *,
    source: str = "cv_kda",
    event_type: str = "kill",
    text: str | None = None,
    start: float = 10.0,
    end: float = 20.0,
) -> dict:
    """Build a test candidate with placeholder data."""
    meta = {"rationale": "test candidate"}
    if text:
        meta["text"] = text
    return {
        "id": cid,
        "source": source,
        "event_type": event_type,
        "start_seconds": start,
        "end_seconds": end,
        "confidence": 0.9,
        "metadata": meta,
    }


class FakeJevScorer:
    """Fake Jev scorer for testing. Returns pre-configured scores."""

    def __init__(self, scores: dict[str, JevScores | None]):
        self.scores = scores
        self.called_with: list[dict] = []

    def score_candidates(self, candidates: list[dict]) -> tuple[list[JevScores | None], int]:
        self.called_with = candidates
        return [self.scores.get(c["id"]) for c in candidates], len(candidates) * 100


class FakeHaikuRanker:
    """Fake Haiku ranker for testing. Records which candidates it receives."""

    def __init__(self, results: dict[str, RankedCandidate] | None = None):
        self.results = results or {}
        self.called_with: list[dict] = []

    def rank(
        self, game: str | None, candidates: list[dict]
    ) -> tuple[list[RankedCandidate], int, int]:
        self.called_with = candidates
        out = []
        for c in candidates:
            if c["id"] in self.results:
                out.append(self.results[c["id"]])
            else:
                out.append(
                    RankedCandidate(
                        candidate_id=c["id"],
                        keep=True,
                        funny_score=0.5,
                        hype_score=0.7,
                        story_score=0.3,
                        suggested_start_seconds=c["start_seconds"] - 2,
                        suggested_end_seconds=c["end_seconds"] + 2,
                        reason="Haiku ranked this candidate",
                    )
                )
        return out, len(candidates) * 500, len(candidates) * 100


def _confident_keep(probability: float = 0.9) -> JevScores:
    return JevScores(
        keep=True,
        keep_probability=probability,
        funny_score=6,
        hype_score=8,
        story_score=4,
        funny_probability=0.8,
        hype_probability=0.9,
        story_probability=0.7,
    )


def _confident_reject(probability: float = 0.9) -> JevScores:
    return JevScores(
        keep=False,
        keep_probability=probability,
        funny_score=2,
        hype_score=3,
        story_score=1,
        funny_probability=0.8,
        hype_probability=0.9,
        story_probability=0.7,
    )


def _unsure() -> JevScores:
    return JevScores(
        keep=True,
        keep_probability=0.55,
        funny_score=5,
        hype_score=5,
        story_score=5,
        funny_probability=0.5,
        hype_probability=0.5,
        story_probability=0.5,
    )


def _rules_fallback() -> JevScores:
    return JevScores(
        keep=True,
        keep_probability=1.0,
        funny_score=5,
        hype_score=5,
        story_score=5,
        funny_probability=1.0,
        hype_probability=1.0,
        story_probability=1.0,
        from_rules_fallback=True,
    )


class TestConfidenceRouting:
    """Test that candidates are routed correctly based on Jev confidence."""

    def test_confident_keep_uses_jev_scores_and_haiku_timing(self):
        candidates = [_candidate("c1", event_type="ace")]
        scorer = FakeJevScorer({"c1": _confident_keep()})
        ranker = FakeHaikuRanker()

        result = cascade_rank(
            "league",
            candidates,
            jev_scorer=scorer,
            haiku_ranker=ranker,
            keep_threshold=0.8,
        )

        assert result.stats.jev_kept == 1
        assert result.stats.jev_decided == 1
        assert len(ranker.called_with) == 1
        assert ranker.called_with[0]["id"] == "c1"

        ranked = result.rankings[0]
        assert ranked.candidate_id == "c1"
        assert ranked.keep is True
        assert ranked.funny_score == 0.6
        assert ranked.hype_score == 0.8
        assert ranked.suggested_start_seconds == 8.0
        assert "Haiku" in ranked.reason

    def test_confident_reject_is_dropped_entirely(self):
        candidates = [_candidate("c1"), _candidate("c2")]
        scorer = FakeJevScorer(
            {
                "c1": _confident_reject(),
                "c2": _confident_keep(),
            }
        )
        ranker = FakeHaikuRanker()

        result = cascade_rank(
            "league",
            candidates,
            jev_scorer=scorer,
            haiku_ranker=ranker,
            keep_threshold=0.8,
            reject_threshold=0.8,
        )

        assert result.stats.jev_rejected == 1
        assert result.stats.jev_kept == 1
        assert result.stats.haiku_candidates_saved == 1
        assert result.stats.timing_refine == 1
        assert result.stats.escalated_unsure == 0
        assert len(ranker.called_with) == 1
        assert ranker.called_with[0]["id"] == "c2"
        assert len(result.rankings) == 1
        assert result.rankings[0].candidate_id == "c2"

    def test_unsure_goes_to_haiku(self):
        candidates = [_candidate("c1")]
        scorer = FakeJevScorer({"c1": _unsure()})
        ranker = FakeHaikuRanker()

        result = cascade_rank(
            "league",
            candidates,
            jev_scorer=scorer,
            haiku_ranker=ranker,
            keep_threshold=0.8,
        )

        assert result.stats.jev_decided == 0
        assert result.stats.escalated_unsure == 1
        assert len(ranker.called_with) == 1
        assert ranker.called_with[0]["id"] == "c1"

    def test_mixed_batch_routes_correctly(self):
        candidates = [
            _candidate("keep1", event_type="ace"),
            _candidate("reject1"),
            _candidate("unsure1"),
            _candidate("keep2", event_type="pentakill"),
        ]
        scorer = FakeJevScorer(
            {
                "keep1": _confident_keep(0.95),
                "reject1": _confident_reject(0.92),
                "unsure1": _unsure(),
                "keep2": _confident_keep(0.88),
            }
        )
        ranker = FakeHaikuRanker()

        result = cascade_rank(
            "league",
            candidates,
            jev_scorer=scorer,
            haiku_ranker=ranker,
            keep_threshold=0.8,
            reject_threshold=0.8,
        )

        assert result.stats.total_candidates == 4
        assert result.stats.jev_kept == 2
        assert result.stats.jev_rejected == 1
        assert result.stats.escalated_unsure == 1
        assert result.stats.timing_refine == 2
        assert result.stats.haiku_candidates_saved == 1
        assert result.stats.escalation_rate == 0.25
        haiku_ids = {c["id"] for c in ranker.called_with}
        assert haiku_ids == {"keep1", "unsure1", "keep2"}
        assert "reject1" not in haiku_ids


class TestFailSafe:
    """Test fail-safe behavior: Jev errors or rules fallback -> unsure."""

    def test_jev_error_marks_all_unsure(self):
        candidates = [_candidate("c1"), _candidate("c2")]
        scorer = FakeJevScorer({"c1": None, "c2": None})
        ranker = FakeHaikuRanker()

        result = cascade_rank(
            "league",
            candidates,
            jev_scorer=scorer,
            haiku_ranker=ranker,
        )

        assert result.stats.jev_decided == 0
        assert result.stats.escalated_unsure == 2
        assert len(ranker.called_with) == 2

    def test_rules_fallback_marks_unsure(self):
        candidates = [_candidate("c1")]
        scorer = FakeJevScorer({"c1": _rules_fallback()})
        ranker = FakeHaikuRanker()

        result = cascade_rank(
            "league",
            candidates,
            jev_scorer=scorer,
            haiku_ranker=ranker,
        )

        assert result.stats.jev_decided == 0
        assert result.stats.escalated_unsure == 1
        assert len(ranker.called_with) == 1

    def test_thin_signals_audio_peak_always_unsure(self):
        candidates = [_candidate("c1", source="audio_peak", event_type="audio_peak")]
        scorer = FakeJevScorer({"c1": _confident_keep(0.95)})
        ranker = FakeHaikuRanker()

        result = cascade_rank(
            "league",
            candidates,
            jev_scorer=scorer,
            haiku_ranker=ranker,
        )

        assert result.stats.jev_decided == 0
        assert result.stats.escalated_unsure == 1

    def test_thin_signals_transcript_keyword_always_unsure(self):
        candidates = [_candidate("c1", source="transcript_keyword", event_type="funny_callout")]
        scorer = FakeJevScorer({"c1": _confident_reject(0.95)})
        ranker = FakeHaikuRanker()

        result = cascade_rank(
            "league",
            candidates,
            jev_scorer=scorer,
            haiku_ranker=ranker,
        )

        assert result.stats.jev_decided == 0
        assert result.stats.jev_rejected == 0
        assert result.stats.escalated_unsure == 1

    def test_promoted_audio_peak_can_be_confident(self):
        """Audio peaks promoted to a visible event type can be confidently decided."""
        c = _candidate("c1", source="audio_peak", event_type="kill")
        c["metadata"]["promoted_from"] = "audio_peak"
        c["metadata"]["promoted_by_source"] = "cv_kda"

        scorer = FakeJevScorer({"c1": _confident_keep(0.95)})
        ranker = FakeHaikuRanker()

        result = cascade_rank(
            "league",
            [c],
            jev_scorer=scorer,
            haiku_ranker=ranker,
        )

        assert result.stats.jev_kept == 1
        assert result.stats.jev_decided == 1


class TestThresholds:
    """Test threshold behavior."""

    def test_below_keep_threshold_is_unsure(self):
        candidates = [_candidate("c1")]
        scorer = FakeJevScorer({"c1": _confident_keep(0.75)})
        ranker = FakeHaikuRanker()

        result = cascade_rank(
            "league",
            candidates,
            jev_scorer=scorer,
            haiku_ranker=ranker,
            keep_threshold=0.8,
        )

        assert result.stats.jev_decided == 0
        assert result.stats.escalated_unsure == 1

    def test_below_reject_threshold_is_unsure(self):
        candidates = [_candidate("c1")]
        scorer = FakeJevScorer({"c1": _confident_reject(0.75)})
        ranker = FakeHaikuRanker()

        result = cascade_rank(
            "league",
            candidates,
            jev_scorer=scorer,
            haiku_ranker=ranker,
            reject_threshold=0.8,
        )

        assert result.stats.jev_decided == 0
        assert result.stats.escalated_unsure == 1

    def test_custom_thresholds_are_respected(self):
        candidates = [_candidate("c1"), _candidate("c2")]
        scorer = FakeJevScorer(
            {
                "c1": _confident_keep(0.65),
                "c2": _confident_reject(0.65),
            }
        )
        ranker = FakeHaikuRanker()

        result = cascade_rank(
            "league",
            candidates,
            jev_scorer=scorer,
            haiku_ranker=ranker,
            keep_threshold=0.6,
            reject_threshold=0.6,
        )

        assert result.stats.jev_kept == 1
        assert result.stats.jev_rejected == 1


class TestPrivacyBoundary:
    """Test that the box path never includes transcript fields."""

    def test_pc_path_includes_transcript_text(self):
        c = _candidate("c1", text="this is transcript text")
        state = build_candidate_state_pc(c)
        assert state["candidate"]["text"] == "this is transcript text"
        assert state_contains_transcript(state)

    def test_box_path_excludes_transcript_text(self):
        c = _candidate("c1", text="this is transcript text")
        state = build_candidate_state_box(c)
        assert "text" not in state["candidate"] or state["candidate"].get("text") is None
        assert not state_contains_transcript(state)

    def test_box_path_keeps_non_transcript_metadata(self):
        c = _candidate("c1")
        c["metadata"]["kills"] = 3
        c["metadata"]["deaths"] = 1
        c["metadata"]["assists"] = 2
        state = build_candidate_state_box(c)
        assert state["candidate"]["kills"] == 3
        assert state["candidate"]["deaths"] == 1
        assert state["candidate"]["assists"] == 2
        assert not state_contains_transcript(state)

    def test_state_contains_transcript_detects_all_fields(self):
        for field in ["text", "transcript_text", "transcript"]:
            state = {"candidate": {field: "some text"}}
            assert state_contains_transcript(state), f"Should detect {field}"

    def test_box_mode_scorer_uses_box_builder(self):
        """Verify that BOX mode scorer uses the box state builder."""
        states_built = []

        def capturing_ask_many(items):
            for _q, state in items:
                states_built.append(state)
            return [
                Answer(value=True, probability=0.9, backend="jev"),
                Answer(value=5, probability=0.8, backend="jev"),
                Answer(value=5, probability=0.8, backend="jev"),
                Answer(value=5, probability=0.8, backend="jev"),
            ]

        scorer = DefaultJevScorer(ask_many_fn=capturing_ask_many, mode=CascadeMode.BOX)
        c = _candidate("c1", text="transcript text should not appear")
        scorer.score_candidates([c])

        assert len(states_built) == 4
        for state in states_built:
            assert not state_contains_transcript(state), "BOX mode leaked transcript"

    def test_cascade_rank_with_box_mode(self):
        """Integration test: cascade_rank in BOX mode never sends transcripts."""
        states_seen = []

        def capturing_ask_many(items):
            for _q, state in items:
                states_seen.append(state)
            return [
                Answer(value=True, probability=0.6, backend="jev"),
                Answer(value=5, probability=0.8, backend="jev"),
                Answer(value=5, probability=0.8, backend="jev"),
                Answer(value=5, probability=0.8, backend="jev"),
            ]

        scorer = DefaultJevScorer(ask_many_fn=capturing_ask_many, mode=CascadeMode.BOX)
        ranker = FakeHaikuRanker()

        c = _candidate("c1", text="secret transcript text")
        cascade_rank(
            "league",
            [c],
            jev_scorer=scorer,
            haiku_ranker=ranker,
            mode=CascadeMode.BOX,
        )

        for state in states_seen:
            assert not state_contains_transcript(state), "BOX cascade leaked transcript"


class TestIntegration:
    """Integration tests showing end-to-end cascade behavior."""

    def test_only_unsure_candidates_reach_haiku(self):
        """The key integration test: Haiku only sees unsure + keeps-needing-timing."""
        candidates = [
            _candidate("confident_keep", event_type="ace"),
            _candidate("confident_reject"),
            _candidate("unsure"),
            _candidate("audio_only", source="audio_peak", event_type="audio_peak"),
        ]
        scorer = FakeJevScorer(
            {
                "confident_keep": _confident_keep(0.95),
                "confident_reject": _confident_reject(0.92),
                "unsure": _unsure(),
                "audio_only": _confident_reject(0.99),
            }
        )
        ranker = FakeHaikuRanker()

        result = cascade_rank(
            "league",
            candidates,
            jev_scorer=scorer,
            haiku_ranker=ranker,
            keep_threshold=0.8,
            reject_threshold=0.8,
        )

        haiku_ids = {c["id"] for c in ranker.called_with}
        assert "confident_reject" not in haiku_ids
        assert "confident_keep" in haiku_ids
        assert "unsure" in haiku_ids
        assert "audio_only" in haiku_ids

        assert result.stats.haiku_candidates_saved == 1
        assert result.stats.escalated_unsure == 2
        assert result.stats.timing_refine == 1
        assert result.stats.escalation_rate == 0.5

    def test_empty_candidates_returns_empty_result(self):
        result = cascade_rank("league", [], jev_scorer=FakeJevScorer({}))
        assert result.rankings == []
        assert result.stats.total_candidates == 0

    def test_escalation_rate_calculation(self):
        candidates = [_candidate(f"c{i}") for i in range(10)]
        scores = {f"c{i}": _confident_reject(0.95) if i < 3 else _unsure() for i in range(10)}
        scorer = FakeJevScorer(scores)
        ranker = FakeHaikuRanker()

        result = cascade_rank(
            "league",
            candidates,
            jev_scorer=scorer,
            haiku_ranker=ranker,
            reject_threshold=0.8,
        )

        assert result.stats.jev_rejected == 3
        assert result.stats.haiku_candidates_saved == 3
        assert result.stats.escalated_unsure == 7
        assert result.stats.escalation_rate == 0.7

    def test_haiku_call_skipped_when_all_rejected(self):
        """When all candidates are confidently rejected, haiku_call_skipped=1."""
        candidates = [_candidate("c1"), _candidate("c2")]
        scorer = FakeJevScorer(
            {
                "c1": _confident_reject(0.95),
                "c2": _confident_reject(0.95),
            }
        )
        ranker = FakeHaikuRanker()

        result = cascade_rank(
            "league",
            candidates,
            jev_scorer=scorer,
            haiku_ranker=ranker,
            reject_threshold=0.8,
        )

        assert result.stats.haiku_call_skipped == 1
        assert len(ranker.called_with) == 0

    def test_haiku_call_not_skipped_when_some_unsure(self):
        """When some candidates need Haiku, haiku_call_skipped=0."""
        candidates = [_candidate("c1")]
        scorer = FakeJevScorer({"c1": _unsure()})
        ranker = FakeHaikuRanker()

        result = cascade_rank(
            "league",
            candidates,
            jev_scorer=scorer,
            haiku_ranker=ranker,
        )

        assert result.stats.haiku_call_skipped == 0
        assert len(ranker.called_with) == 1


class TestCostLogging:
    """Test cost logging functionality."""

    def test_cost_log_records_calls(self):
        cost_log = CostLog(video_id="test-video")
        cost_log.record_call(model="claude-haiku-4-5", input_tokens=1000, output_tokens=200)
        cost_log.record_call(model="jev-latest", input_tokens=500, cost_dollars=None)

        assert len(cost_log.calls) == 2
        assert cost_log.total_input_tokens == 1500
        assert cost_log.total_output_tokens == 200

    def test_cost_log_total_with_unknown(self):
        cost_log = CostLog()
        cost_log.record_call(model="haiku", cost_dollars=0.01)
        cost_log.record_call(model="jev", cost_dollars=None)

        assert cost_log.total_cost_dollars is None

    def test_cost_log_total_all_known(self):
        cost_log = CostLog()
        cost_log.record_call(model="haiku", cost_dollars=0.01)
        cost_log.record_call(model="haiku", cost_dollars=0.02)

        assert cost_log.total_cost_dollars == 0.03

    def test_cost_log_to_dict(self):
        cost_log = CostLog(video_id="v1", run_id="r1")
        cost_log.record_call(
            model="claude-haiku-4-5",
            input_tokens=1000,
            output_tokens=200,
            cost_dollars=0.001,
        )

        d = cost_log.to_dict()
        assert d["video_id"] == "v1"
        assert d["run_id"] == "r1"
        assert len(d["calls"]) == 1
        assert d["calls"][0]["model"] == "claude-haiku-4-5"
        assert d["summary"]["total_calls"] == 1

    def test_cascade_rank_produces_cost_log(self):
        scorer = FakeJevScorer({"c1": _unsure()})
        ranker = FakeHaikuRanker()

        result = cascade_rank(
            "league",
            [_candidate("c1")],
            jev_scorer=scorer,
            haiku_ranker=ranker,
            video_id="test-video",
            run_id="test-run",
        )

        assert result.cost_log.video_id == "test-video"
        assert result.cost_log.run_id == "test-run"
        assert len(result.cost_log.calls) == 2

    def test_cascade_metrics_in_cost_log(self):
        """Cost log should include cascade metrics from stats."""
        candidates = [
            _candidate("keep1", event_type="ace"),
            _candidate("reject1"),
            _candidate("unsure1"),
        ]
        scorer = FakeJevScorer(
            {
                "keep1": _confident_keep(0.95),
                "reject1": _confident_reject(0.92),
                "unsure1": _unsure(),
            }
        )
        ranker = FakeHaikuRanker()

        result = cascade_rank(
            "league",
            candidates,
            jev_scorer=scorer,
            haiku_ranker=ranker,
            keep_threshold=0.8,
            reject_threshold=0.8,
        )

        d = result.cost_log.to_dict()
        assert "cascade" in d
        cascade = d["cascade"]
        assert cascade["total_candidates"] == 3
        assert cascade["jev_decided"] == 2
        assert cascade["jev_kept"] == 1
        assert cascade["jev_rejected"] == 1
        assert cascade["escalated_unsure"] == 1
        assert cascade["timing_refine"] == 1
        assert cascade["haiku_candidates_saved"] == 1
        assert cascade["haiku_call_skipped"] == 0
        assert cascade["escalation_rate"] == round(1 / 3, 4)

    def test_cost_log_merge(self):
        log1 = CostLog()
        log1.record_call(model="a", input_tokens=100)
        log2 = CostLog()
        log2.record_call(model="b", input_tokens=200)

        log1.merge(log2)
        assert len(log1.calls) == 2
        assert log1.total_input_tokens == 300


class TestEntryPoint:
    """Test the rank_candidates_with_cascade entry point."""

    def test_disabled_cascade_calls_direct_ranker(self, monkeypatch):
        monkeypatch.setattr(settings, "grokbot_jev_ranker_cascade_enabled", False)

        called = []

        def fake_rank(game, candidates):
            called.append((game, candidates))
            return [
                RankedCandidate(
                    candidate_id=c["id"],
                    keep=True,
                    funny_score=0.5,
                    hype_score=0.5,
                    story_score=0.5,
                    suggested_start_seconds=0,
                    suggested_end_seconds=10,
                    reason="direct",
                )
                for c in candidates
            ]

        import ai_video_editor.ranker as ranker_module

        monkeypatch.setattr(ranker_module, "rank_candidates", fake_rank)

        candidates = [_candidate("c1")]
        result = rank_candidates_with_cascade("league", candidates)

        assert len(called) == 1
        assert called[0][0] == "league"
        assert result[0].reason == "direct"
