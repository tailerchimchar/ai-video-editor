"""Frame strip / peaks planning, argv and idempotent caching (no ffmpeg)."""

from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

from ai_video_editor.narration import strips


def test_strip_plan():
    assert strips.strip_plan(792.9) == (4.0, 199)
    assert strips.strip_plan(60.0) == (1.0, 60)


def test_sprite_cmd_has_tile_filter():
    cmd = strips.sprite_cmd(Path("v.mp4"), Path("o.jpg"), 4.0, 199)
    assert "fps=1/4.0,scale=160:-2,tile=199x1" in cmd
    assert cmd[-1] == "o.jpg"


def test_peaks_cmd_is_mono_8k_pcm_pipe():
    cmd = strips.peaks_cmd(Path("v.mp4"))
    assert cmd[-1] == "pipe:1"
    assert cmd[cmd.index("-ac") : cmd.index("-ac") + 4] == ["-ac", "1", "-ar", "8000"]


def _pcm(values: list[int]) -> bytes:
    return struct.pack(f"<{len(values)}h", *values)


def test_peaks_from_pcm_alternating():
    quiet, loud = [0] * 800, [32767] * 800
    raw = _pcm((quiet + loud) * 10)  # 2 s at 8 kHz
    peaks = strips.peaks_from_pcm(raw)
    assert len(peaks) == 20
    assert peaks[:3] == [0.0, 1.0, 0.0]


def test_peaks_from_pcm_edges():
    assert strips.peaks_from_pcm(b"") == []
    assert strips.peaks_from_pcm(_pcm([0] * 1600)) == [0.0, 0.0]
    # normalised to the global max
    assert strips.peaks_from_pcm(_pcm([100] * 800 + [200] * 800)) == [0.5, 1.0]


def test_ensure_frames_renders_once(tmp_path):
    calls: list[list[str]] = []

    def run(cmd):
        calls.append(cmd)
        Path(cmd[-1]).write_bytes(b"jpg")
        return True, None

    cache = tmp_path / "cache"
    kw = dict(duration_s=60.0, run=run, probe_size=lambda p: (160, 90))
    a = strips.ensure_frames(Path("v.mp4"), cache, "fp1", **kw)
    b = strips.ensure_frames(Path("v.mp4"), cache, "fp1", **kw)
    assert len(calls) == 1
    assert a == b
    assert (a.every_s, a.count, a.cols, a.tile_w, a.tile_h) == (1.0, 60, 60, 160, 90)
    assert a.sprite.read_bytes() == b"jpg"
    assert json.loads(a.meta.read_text())["tile_h"] == 90
    assert not (cache / ".lock-fp1").exists()


def test_ensure_frames_failure_leaves_no_lock(tmp_path):
    cache = tmp_path / "cache"
    with pytest.raises(RuntimeError, match="boom"):
        strips.ensure_frames(
            Path("v.mp4"),
            cache,
            "fp",
            duration_s=10,
            run=lambda c: (False, "boom"),
            probe_size=lambda p: (1, 1),
        )
    assert not (cache / ".lock-fp").exists()


def test_ensure_peaks_renders_once(tmp_path):
    calls = []

    def cap(cmd):
        calls.append(cmd)
        return _pcm([0] * 800 + [1000] * 800)

    cache = tmp_path / "cache"
    a = strips.ensure_peaks(Path("v.mp4"), cache, "fp", run_capture=cap)
    b = strips.ensure_peaks(Path("v.mp4"), cache, "fp", run_capture=cap)
    assert len(calls) == 1
    assert a == b and a.peaks == [0.0, 1.0] and a.rate_hz == 10


def test_waits_for_other_renderer(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / ".lock-fp").write_text("")
    out = cache / "peaks-fp.json"
    sleeps = []

    def fake_sleep(s):
        sleeps.append(s)
        out.write_text('{"rate_hz": 10, "peaks": [0.5]}')

    monkeypatch.setattr(strips.time, "sleep", fake_sleep)
    got = strips.ensure_peaks(
        Path("v.mp4"), cache, "fp", run_capture=lambda c: pytest.fail("must not render")
    )
    assert got.peaks == [0.5] and sleeps


def test_lock_wait_times_out(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / ".lock-fp").write_text("")
    monkeypatch.setattr(strips, "LOCK_WAIT_S", 0.0)
    with pytest.raises(RuntimeError, match="timed out"):
        strips.ensure_peaks(Path("v.mp4"), cache, "fp", run_capture=lambda c: b"")
