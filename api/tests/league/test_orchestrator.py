"""LoL orchestrator: fight bounds follow the calibrated offset; drift check
widens the pre-pad; the cache records the final offset (Jhin round T1)."""

import json

import numpy as np
import pytest

from ai_video_editor.league import orchestrator as orch


def _row(gc_s: float, fight: tuple[float, float] | None, game_end: float | None = 900.0):
    meta = {"riot_event_ms": int(gc_s * 1000), "anchor_seconds": gc_s}
    if fight is not None:
        meta |= {
            "fight_start_s": fight[0],
            "fight_end_s": fight[1],
            "game_end_s": game_end,
            "match_confidence": "high",
        }
    return {"event_type": "kill", "confidence": 0.95, "metadata": meta}


@pytest.fixture
def cfg(monkeypatch, tmp_path):
    from ai_video_editor.config import settings

    monkeypatch.setattr(settings, "workspace_dir", tmp_path)
    monkeypatch.setattr(settings, "riot_offset_override_seconds", None)
    monkeypatch.setattr(settings, "riot_sync_offset_seconds", 0.0)
    monkeypatch.setattr(settings, "riot_offset_min_quality", 0.0)
    return tmp_path


def _asset():
    return {"id": "a1", "path": "unused.mp4"}


def test_fight_bounds_shift_with_calibrated_offset(cfg, monkeypatch):
    rows = [_row(100.0, (95.0, 110.0)), _row(400.0, (400.0, 400.0))]
    monkeypatch.setattr(orch, "detect_riot_events", lambda a, d: (rows, "ok"))
    monkeypatch.setattr(orch, "align_offset", lambda kills, energy: (20.0, 5.0))
    out, status = orch.gather_lol_candidates(_asset(), 1000.0, np.zeros(1000))
    assert status == "ok"
    md = out[0]["metadata"]
    assert md["anchor_seconds"] == 120.0
    assert (md["fight_start_s"], md["fight_end_s"], md["game_end_s"]) == (115.0, 130.0, 920.0)
    assert "pre_pad_s" not in md  # halves agree -> default pad


def test_rows_without_fight_bounds_unchanged(cfg, monkeypatch):
    rows = [_row(100.0, None)]
    monkeypatch.setattr(orch, "detect_riot_events", lambda a, d: (rows, "ok"))
    monkeypatch.setattr(orch, "align_offset", lambda kills, energy: (20.0, 5.0))
    out, _ = orch.gather_lol_candidates(_asset(), 1000.0, np.zeros(1000))
    md = out[0]["metadata"]
    for k in ("fight_start_s", "fight_end_s", "game_end_s", "pre_pad_s"):
        assert k not in md


def test_drift_over_three_seconds_widens_pre_pad(cfg, monkeypatch, caplog):
    rows = [_row(t, (t, t)) for t in (100.0, 200.0, 600.0, 700.0)]
    monkeypatch.setattr(orch, "detect_riot_events", lambda a, d: (rows, "ok"))

    def fake_align(kills, energy):
        if len(kills) == 4:
            return 10.0, 5.0  # global lock
        return (10.0, 5.0) if max(kills) < 500 else (15.0, 5.0)  # second half drifted

    monkeypatch.setattr(orch, "align_offset", fake_align)
    with caplog.at_level("WARNING"):
        out, _ = orch.gather_lol_candidates(_asset(), 1000.0, np.zeros(1000))
    assert all(r["metadata"]["pre_pad_s"] == 8.0 for r in out)
    assert any("drift" in m.lower() for m in caplog.messages)


def test_drift_check_failure_never_breaks_candidates(cfg, monkeypatch):
    rows = [_row(t, (t, t)) for t in (100.0, 200.0, 600.0, 700.0)]
    monkeypatch.setattr(orch, "detect_riot_events", lambda a, d: (rows, "ok"))
    calls = []

    def flaky(kills, energy):
        calls.append(1)
        if len(calls) > 1:
            raise RuntimeError("boom")
        return 10.0, 5.0

    monkeypatch.setattr(orch, "align_offset", flaky)
    out, status = orch.gather_lol_candidates(_asset(), 1000.0, np.zeros(1000))
    assert status == "ok" and len(out) == 4
    assert "pre_pad_s" not in out[0]["metadata"]


def test_calibrated_offset_recorded_in_cache(cfg, monkeypatch):
    d = cfg / "riot" / "a1"
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps({"confidence": "high", "offset_s": 0.0}))
    monkeypatch.setattr(orch, "detect_riot_events", lambda a, d: ([_row(100.0, None)], "ok"))
    monkeypatch.setattr(orch, "align_offset", lambda kills, energy: (20.0, 5.0))
    orch.gather_lol_candidates(_asset(), 1000.0, np.zeros(1000))
    assert json.loads((d / "meta.json").read_text())["offset_s"] == 20.0


def test_manual_override_skips_drift_check(cfg, monkeypatch):
    from ai_video_editor.config import settings

    monkeypatch.setattr(settings, "riot_offset_override_seconds", 12.0)
    rows = [_row(t, (t, t)) for t in (100.0, 200.0, 600.0, 700.0)]
    monkeypatch.setattr(orch, "detect_riot_events", lambda a, d: (rows, "ok"))
    monkeypatch.setattr(orch, "_drift_pre_pad", lambda *a: pytest.fail("drift ran"))
    out, _ = orch.gather_lol_candidates(_asset(), 1000.0, np.zeros(1000))
    assert out[0]["metadata"]["offset_source"] == "manual_override"
    assert out[0]["metadata"]["fight_start_s"] == 112.0
