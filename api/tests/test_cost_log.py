"""Tests for the per-video cost logging module."""

import json
import logging

from ai_video_editor.cost_log import CostLog, ModelCall


class TestModelCall:
    """Test ModelCall dataclass."""

    def test_to_dict_full(self):
        call = ModelCall(
            model="claude-haiku-4-5",
            input_tokens=1000,
            output_tokens=200,
            frame_count=8,
            cost_dollars=0.002,
        )
        d = call.to_dict()
        assert d == {
            "model": "claude-haiku-4-5",
            "input_tokens": 1000,
            "output_tokens": 200,
            "frame_count": 8,
            "cost_dollars": 0.002,
        }

    def test_to_dict_minimal(self):
        call = ModelCall(model="jev-latest")
        d = call.to_dict()
        assert d == {"model": "jev-latest", "cost_dollars": None}

    def test_to_dict_unknown_cost_is_null(self):
        call = ModelCall(model="test", input_tokens=100, cost_dollars=None)
        d = call.to_dict()
        assert d["cost_dollars"] is None


class TestCostLog:
    """Test CostLog class."""

    def test_empty_log(self):
        log = CostLog()
        assert log.calls == []
        assert log.total_cost_dollars == 0.0
        assert log.total_input_tokens == 0
        assert log.total_output_tokens == 0
        assert log.total_frames == 0

    def test_record_call(self):
        log = CostLog(video_id="v1")
        log.record_call(
            model="claude-haiku-4-5",
            input_tokens=1000,
            output_tokens=200,
            cost_dollars=0.002,
        )
        assert len(log.calls) == 1
        assert log.calls[0].model == "claude-haiku-4-5"
        assert log.calls[0].input_tokens == 1000
        assert log.calls[0].output_tokens == 200
        assert log.calls[0].cost_dollars == 0.002

    def test_record_multiple_calls(self):
        log = CostLog()
        log.record_call(model="haiku", input_tokens=1000, output_tokens=200)
        log.record_call(model="jev", input_tokens=500)
        log.record_call(model="vlm", frame_count=30)

        assert len(log.calls) == 3
        assert log.total_input_tokens == 1500
        assert log.total_output_tokens == 200
        assert log.total_frames == 30

    def test_total_cost_all_known(self):
        log = CostLog()
        log.record_call(model="a", cost_dollars=0.01)
        log.record_call(model="b", cost_dollars=0.02)
        log.record_call(model="c", cost_dollars=0.03)

        assert log.total_cost_dollars == 0.06

    def test_total_cost_with_unknown_returns_none(self):
        log = CostLog()
        log.record_call(model="a", cost_dollars=0.01)
        log.record_call(model="b", cost_dollars=None)
        log.record_call(model="c", cost_dollars=0.03)

        assert log.total_cost_dollars is None

    def test_to_dict(self):
        log = CostLog(video_id="video-123", run_id="run-456")
        log.record_call(model="haiku", input_tokens=1000, output_tokens=200, cost_dollars=0.01)
        log.record_call(model="jev", input_tokens=500, cost_dollars=None)

        d = log.to_dict()
        assert d["video_id"] == "video-123"
        assert d["run_id"] == "run-456"
        assert len(d["calls"]) == 2
        assert d["summary"]["total_calls"] == 2
        assert d["summary"]["total_input_tokens"] == 1500
        assert d["summary"]["total_output_tokens"] == 200
        assert d["summary"]["total_frames"] == 0
        assert d["summary"]["total_cost_dollars"] is None

    def test_emit_logs_structured_line(self, caplog):
        log = CostLog(video_id="v1", run_id="r1")
        log.record_call(model="haiku", input_tokens=1000, cost_dollars=0.01)

        with caplog.at_level(logging.INFO):
            log.emit()

        assert len(caplog.records) == 1
        record = caplog.records[0]
        assert "cost_record" in record.message
        assert "v1" in record.message
        assert "r1" in record.message
        assert "0.01" in record.message
        assert "{" in record.message

    def test_emit_handles_none_cost(self, caplog):
        log = CostLog(video_id="v1")
        log.record_call(model="jev", input_tokens=100, cost_dollars=None)

        with caplog.at_level(logging.INFO):
            log.emit()

        assert "None" in caplog.records[0].message

    def test_merge(self):
        log1 = CostLog(video_id="v1")
        log1.record_call(model="a", input_tokens=100, cost_dollars=0.01)

        log2 = CostLog()
        log2.record_call(model="b", input_tokens=200, cost_dollars=0.02)
        log2.record_call(model="c", input_tokens=300, cost_dollars=0.03)

        log1.merge(log2)

        assert len(log1.calls) == 3
        assert log1.total_input_tokens == 600
        assert log1.total_cost_dollars == 0.06
        assert log1.video_id == "v1"

    def test_to_dict_is_json_serializable(self):
        log = CostLog(video_id="v1", run_id="r1")
        log.record_call(model="haiku", input_tokens=1000, output_tokens=200, cost_dollars=0.01)
        log.record_call(model="jev", input_tokens=500, cost_dollars=None)

        d = log.to_dict()
        json_str = json.dumps(d)
        parsed = json.loads(json_str)

        assert parsed["video_id"] == "v1"
        assert parsed["summary"]["total_cost_dollars"] is None

    def test_set_cascade_metrics(self):
        log = CostLog(video_id="v1")
        log.set_cascade_metrics(
            total_candidates=10,
            jev_decided=4,
            jev_kept=2,
            jev_rejected=2,
            escalated_unsure=6,
            timing_refine=2,
            escalation_rate=0.6,
            haiku_candidates_saved=2,
            haiku_call_skipped=0,
        )

        assert log.cascade_metrics is not None
        assert log.cascade_metrics.total_candidates == 10
        assert log.cascade_metrics.escalation_rate == 0.6

    def test_cascade_metrics_in_to_dict(self):
        log = CostLog(video_id="v1")
        log.set_cascade_metrics(
            total_candidates=10,
            jev_decided=4,
            jev_kept=2,
            jev_rejected=2,
            escalated_unsure=6,
            timing_refine=2,
            escalation_rate=0.6,
            haiku_candidates_saved=2,
            haiku_call_skipped=0,
        )

        d = log.to_dict()
        assert "cascade" in d
        assert d["cascade"]["total_candidates"] == 10
        assert d["cascade"]["escalated_unsure"] == 6
        assert d["cascade"]["timing_refine"] == 2
        assert d["cascade"]["haiku_candidates_saved"] == 2
        assert d["cascade"]["escalation_rate"] == 0.6

    def test_to_dict_without_cascade_metrics(self):
        log = CostLog(video_id="v1")
        d = log.to_dict()
        assert "cascade" not in d

    def test_cascade_metrics_json_serializable(self):
        log = CostLog(video_id="v1")
        log.set_cascade_metrics(
            total_candidates=10,
            jev_decided=4,
            jev_kept=2,
            jev_rejected=2,
            escalated_unsure=6,
            timing_refine=2,
            escalation_rate=0.6,
            haiku_candidates_saved=2,
            haiku_call_skipped=0,
        )

        d = log.to_dict()
        json_str = json.dumps(d)
        parsed = json.loads(json_str)

        assert parsed["cascade"]["escalation_rate"] == 0.6
