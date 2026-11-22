"""HP-bar reader: synthetic 1280x720 frames only; ffmpeg is never called."""

import json

import numpy as np
import pytest

from ai_video_editor.cv import hp_bar
from ai_video_editor.cv.hp_bar import HP_BAR_ROI_720P, hp_events, read_owner_hp

GREEN = (30, 170, 50)
RED = (200, 30, 30)
GREY = (90, 90, 90)
WHITE = (250, 250, 250)


def _frame(fill: float, colour=GREEN, shield: float = 0.0, empty=(20, 20, 20)):
    f = np.zeros((720, 1280, 3), dtype=np.uint8)
    x, y, w, h = HP_BAR_ROI_720P["owner"]
    f[y : y + h, x : x + w] = empty
    n = round(w * fill)
    f[y : y + h, x : x + n] = colour
    s = round(w * shield)
    f[y : y + h, x + n : x + n + s] = WHITE
    return f


def test_full_bar():
    assert read_owner_hp(_frame(1.0)) == pytest.approx(1.0, abs=0.02)


def test_half_bar():
    assert read_owner_hp(_frame(0.5)) == pytest.approx(0.5, abs=0.05)


def test_low_red_bar():
    assert read_owner_hp(_frame(0.1, colour=RED)) == pytest.approx(0.1, abs=0.05)


def test_stray_right_column_does_not_inflate():
    f = _frame(0.3)
    x, y, w, h = HP_BAR_ROI_720P["owner"]
    f[y : y + h, x + w - 2 : x + w] = GREEN
    assert read_owner_hp(f) == pytest.approx(0.3, abs=0.05)


def test_small_frame_is_none():
    assert read_owner_hp(np.zeros((100, 100, 3), dtype=np.uint8)) is None


def test_grey_bar_is_none():
    assert read_owner_hp(_frame(1.0, colour=GREY)) is None


def test_absent_bar_is_none():
    assert read_owner_hp(np.zeros((720, 1280, 3), dtype=np.uint8)) is None


def test_shield_ignored():
    got = read_owner_hp(_frame(0.6, shield=0.3))
    assert got is not None and got <= 0.6 + 0.05


def test_events_trade_and_escape():
    samples = [(0.0, 1.0), (1.5, 0.9), (3.0, 0.6), (4.5, 0.5), (6.0, 0.6), (7.5, 0.8)]
    samples += [(30.0, 0.9), (31.5, 0.6), (33.0, 0.3), (34.5, 0.12), (36.0, 0.5), (60.0, 1.0)]
    ev = hp_events(samples, deaths_s=[])
    kinds = [e["type"] for e in ev]
    assert kinds.count("trade") == 2
    trades = [e for e in ev if e["type"] == "trade"]
    assert trades[0]["min_hp"] == pytest.approx(0.5)
    assert trades[1]["min_hp"] == pytest.approx(0.12)
    assert kinds.count("escape") == 1


def test_escape_cancelled_by_death():
    samples = [(0.0, 0.8), (1.5, 0.1), (3.0, 0.1)]
    assert [e for e in hp_events(samples, deaths_s=[5.0]) if e["type"] == "escape"] == []


def test_no_events_on_flat_or_none():
    assert hp_events([(0.0, 1.0), (1.5, None), (3.0, 1.0)], deaths_s=[]) == []


def test_shadow_writes_json(tmp_path, monkeypatch):
    monkeypatch.setattr(hp_bar.settings, "workspace_dir", tmp_path)
    monkeypatch.setattr(
        hp_bar, "scan_hp", lambda p, *, step_s: [(0.0, 1.0), (1.5, 0.6), (3.0, 0.5), (4.5, 0.8)]
    )
    out = hp_bar.scan_hp_shadow("abc", video_path="x.mp4")
    data = json.loads((tmp_path / "hp_shadow" / "abc.json").read_text())
    assert data["asset_id"] == "abc" and out == data["events"]
    assert any(e["type"] == "trade" for e in data["events"])


def test_shadow_records_error_when_no_samples(tmp_path, monkeypatch):
    monkeypatch.setattr(hp_bar.settings, "workspace_dir", tmp_path)
    monkeypatch.setattr(hp_bar, "scan_hp", lambda p, *, step_s: [])
    hp_bar.scan_hp_shadow("bad", video_path="x.mp4")
    data = json.loads((tmp_path / "hp_shadow" / "bad.json").read_text())
    assert "error" in data and data["events"] == []


_TRADE = {"type": "trade", "start_s": 1.0, "end_s": 5.0, "min_hp": 0.4}


def test_service_shadow_only_scans_but_no_rows(monkeypatch):
    from ai_video_editor.candidates import service

    calls = []
    monkeypatch.setattr(service, "scan_hp_shadow", lambda *a, **k: calls.append(1) or [_TRADE])
    monkeypatch.setattr(service.settings, "hp_reader_enabled", False)
    monkeypatch.setattr(service.settings, "hp_reader_shadow", True)
    assert service.hp_rows_if_enabled("a", "v.mp4", []) == []
    assert calls == [1]


def test_service_enabled_returns_rows(monkeypatch):
    from ai_video_editor.candidates import service

    monkeypatch.setattr(service, "scan_hp_shadow", lambda *a, **k: [_TRADE])
    monkeypatch.setattr(service.settings, "hp_reader_enabled", True)
    rows = service.hp_rows_if_enabled("a", "v.mp4", [])
    assert len(rows) == 1 and rows[0]["source"] == "hp_bar"


def test_service_adds_nothing_when_disabled(monkeypatch):
    from ai_video_editor.candidates import service

    called = []
    monkeypatch.setattr(service, "scan_hp_shadow", lambda *a, **k: called.append(1) or [])
    monkeypatch.setattr(service.settings, "hp_reader_enabled", False)
    monkeypatch.setattr(service.settings, "hp_reader_shadow", False)
    assert service.hp_rows_if_enabled("a", "v.mp4", []) == []
    assert called == []
