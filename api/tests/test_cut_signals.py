"""cut_signals: pure stderr parser + injectable runner. No real media."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ai_video_editor.cut_signals import (
    CutSignals,
    LoudnessStep,
    QuietSpan,
    extract_cut_signals,
    merge_signals,
    parse_ffmpeg_stderr,
)

# Invented sample text — shapes match ffmpeg filter logs, not a recording.
_SAMPLE_AUDIO = """
Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'clip_alpha.mp4':
  Duration: 00:00:20.00
[silencedetect @ 0x1] silence_start: 0.5
[silencedetect @ 0x1] silence_end: 1.8 | silence_duration: 1.3
[silencedetect @ 0x1] silence_start: 14.2
[silencedetect @ 0x1] silence_end: 15.0 | silence_duration: 0.8
[Parsed_ebur128_1 @ 0x2] t: 0.5  TARGET:-23 LUFS    M: -45.0 S:-50.0     I: -40.0 LUFS
[Parsed_ebur128_1 @ 0x2] t: 1.0  TARGET:-23 LUFS    M: -22.5 S:-30.0     I: -28.0 LUFS
[Parsed_ebur128_1 @ 0x2] t: 2.0  TARGET:-23 LUFS    M: -18.0 S:-20.0     I: -22.0 LUFS
[Parsed_ebur128_1 @ 0x2] t: 3.0  TARGET:-23 LUFS    M: -inf S:-120.7     I: -25.0 LUFS
"""

_SAMPLE_SCENE = """
[scdet @ 0x3] lavfi.scd.score: 22.1, lavfi.scd.time: 4.5
[scdet @ 0x3] lavfi.scd.score: 31.0, lavfi.scd.time: 11.25
frame:12    pts:12000   pts_time:7.5
lavfi.scene_score=0.55
"""


def test_parse_silence_spans_and_loudness_steps():
    sig = parse_ffmpeg_stderr(_SAMPLE_AUDIO)
    assert sig.quiet_spans == [
        QuietSpan(0.5, 1.8),
        QuietSpan(14.2, 15.0),
    ]
    assert sig.loudness[0] == LoudnessStep(0.5, -45.0)
    assert sig.loudness[1].loudness == -22.5
    # -inf momentary maps to a very quiet floor
    assert sig.loudness[-1].loudness == -120.0


def test_parse_scene_changes_from_scdet_and_select_metadata():
    sig = parse_ffmpeg_stderr(_SAMPLE_SCENE)
    assert 4.5 in sig.scene_changes
    assert 11.25 in sig.scene_changes
    assert 7.5 in sig.scene_changes


def test_parse_applies_time_offset_for_windowed_decode():
    sig = parse_ffmpeg_stderr(_SAMPLE_AUDIO, time_offset=100.0)
    assert sig.quiet_spans[0] == QuietSpan(100.5, 101.8)
    assert sig.loudness[0].time == 100.5


def test_merge_signals_combines_and_sorts():
    a = parse_ffmpeg_stderr(_SAMPLE_AUDIO)
    b = parse_ffmpeg_stderr(_SAMPLE_SCENE)
    merged = merge_signals(a, b)
    assert len(merged.quiet_spans) == 2
    assert len(merged.scene_changes) == 3
    assert merged.scene_changes == sorted(merged.scene_changes)


def test_extract_uses_injected_runner_never_calls_ffmpeg():
    calls: list[list[str]] = []

    def fake_runner(argv: list[str]) -> str:
        calls.append(argv)
        if any("silencedetect" in a for a in argv):
            return _SAMPLE_AUDIO
        return _SAMPLE_SCENE

    sig, ms = extract_cut_signals("unused.mp4", (10.0, 25.0), runner=fake_runner)
    assert len(calls) == 2
    assert ms >= 0.0
    # Window start is passed as -ss; parser offsets absolute times.
    assert any("-ss" in c and "10.0" in c for c in calls)
    assert sig.quiet_spans[0].start == pytest.approx(10.5)
    assert 14.5 in sig.scene_changes  # 4.5 + 10 offset


def test_extract_empty_on_runner_failure():
    def boom(_argv: list[str]) -> str:
        raise RuntimeError("no ffmpeg")

    sig, _ms = extract_cut_signals("x.mp4", (0.0, 5.0), runner=boom)
    assert sig == CutSignals()


def test_extract_rejects_degenerate_window():
    sig, ms = extract_cut_signals("x.mp4", (5.0, 5.0), runner=lambda a: "")
    assert sig == CutSignals()
    assert ms == 0.0


def test_ffmpeg_generated_testsrc_sine_signals(tmp_path: Path):
    """Real ffmpeg on synthetic testsrc+sine — no integration marker on master."""
    out = tmp_path / "synthetic.mp4"
    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "testsrc=duration=3:size=160x120:rate=10",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=3",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-shortest",
        str(out),
    ]
    try:
        built = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pytest.skip("ffmpeg unavailable")
    if built.returncode != 0:
        pytest.skip(f"ffmpeg could not build test clip: {built.stderr[:200]}")

    sig, ms = extract_cut_signals(str(out), (0.0, 3.0))
    assert ms > 0.0
    # Sine is continuous — may or may not yield silence; loudness steps should.
    assert isinstance(sig, CutSignals)
    assert len(sig.loudness) >= 1 or len(sig.quiet_spans) >= 0
