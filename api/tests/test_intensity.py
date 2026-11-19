"""Clip intensity scorer: pure function, property-style tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_video_editor.config import settings
from ai_video_editor.intensity import (
    ClipWithIntensity,
    IntensityWeights,
    score_clip,
    score_session,
)


def _ev(event_type: str) -> dict:
    return {"event_type": event_type}


def test_no_signals_scores_zero() -> None:
    assert score_clip([], None, None) == 0.0


def test_monotonic_in_kill_count() -> None:
    scores = [score_clip([_ev("kill")] * n, None, None) for n in range(0, 6)]
    assert scores == sorted(scores)
    assert scores[1] > scores[0]


def test_monotonic_in_multi_kill_tier() -> None:
    tiers = ["kill", "doublekill", "triplekill", "quadrakill", "pentakill"]
    scores = [score_clip([_ev(t)], None, None) for t in tiers]
    assert scores == sorted(scores)
    assert scores[1] > scores[0]
    assert scores[2] > scores[1]


def test_clamped_to_unit_interval() -> None:
    assert score_clip([_ev("pentakill")] * 4, 1.0, 1.0) == 1.0
    assert score_clip([_ev("death")] * 20, None, None) == 0.0


def test_death_without_kill_below_lone_kill() -> None:
    assert score_clip([_ev("death")], None, None) < score_clip([_ev("kill")], None, None)


def test_missing_optional_signals_use_remaining() -> None:
    kills_only = score_clip([_ev("kill")], None, None)
    assert kills_only > 0
    assert score_clip([_ev("kill")], 0.8, 0.9) > kills_only
    assert score_clip([], 0.8, None) > 0
    assert score_clip([], None, 0.9) > 0


def test_unknown_event_types_ignored() -> None:
    assert score_clip([_ev("mystery")], None, None) == 0.0


def test_weights_argument_overrides_defaults() -> None:
    w = IntensityWeights(
        kills=0.5,
        multi_kill_bonus={},
        vlm_payoff=0.0,
        voice=0.0,
        death_penalty=0.0,
    )
    assert score_clip([_ev("kill")], None, 1.0, w) == pytest.approx(0.5)


def test_weights_default_from_settings(monkeypatch) -> None:
    monkeypatch.setattr(settings, "intensity_kill_weight", 0.4)
    monkeypatch.setattr(settings, "intensity_death_penalty", 0.3)
    w = IntensityWeights()
    assert w.kills == 0.4
    assert w.death_penalty == 0.3
    assert score_clip([_ev("kill")], None, None) == pytest.approx(0.4)


def _write_index(folder: Path) -> Path:
    def clip(file, event, start, end, ok=True) -> dict:
        return {
            "file": file,
            "event": event,
            "start_seconds": start,
            "end_seconds": end,
            "ok": ok,
        }

    clips = [
        clip("a.mp4", "kill", 10, 20),
        clip("b.mp4", "pentakill", 100, 115),
        clip("c.mp4", "death", 200, 210),
        clip(None, "kill", 300, 310, ok=False),
    ]
    idx = folder / "index.json"
    idx.write_text(json.dumps({"clips": clips}), encoding="utf-8")
    return idx


def test_score_session_one_result_per_rendered_clip(tmp_path: Path) -> None:
    result = score_session(_write_index(tmp_path))
    assert [c.clip_ref for c in result] == ["a.mp4", "b.mp4", "c.mp4"]
    assert all(isinstance(c, ClipWithIntensity) for c in result)
    assert max(result, key=lambda c: c.intensity).clip_ref == "b.mp4"
    assert (result[1].start, result[1].end) == (100.0, 115.0)


def test_score_session_reads_optional_sidecar(tmp_path: Path) -> None:
    idx = _write_index(tmp_path)
    baseline = {c.clip_ref: c.intensity for c in score_session(idx)}
    (tmp_path / "a.analysis.json").write_text(json.dumps({"payoff_score": 1.0}), encoding="utf-8")
    boosted = {c.clip_ref: c.intensity for c in score_session(idx)}
    assert boosted["a.mp4"] > baseline["a.mp4"]


def test_score_session_tolerates_bad_sidecar_and_missing_index(
    tmp_path: Path,
) -> None:
    idx = _write_index(tmp_path)
    (tmp_path / "a.analysis.json").write_text("{not json", encoding="utf-8")
    assert len(score_session(idx)) == 3
    assert score_session(tmp_path / "missing.json") == []
