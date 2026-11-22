"""Shorts render honours the configured encoder (no hardcoded x264 opts)."""

from __future__ import annotations

import inspect
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ai_video_editor import shorts
from ai_video_editor.config import settings
from ai_video_editor.shorts import ShortClip, ShortPlan, render_short


def _plan() -> ShortPlan:
    clip = ShortClip(
        file="a.mp4",
        path=Path("/tmp/a.mp4"),
        event_type="kill",
        start_seconds=0.0,
        end_seconds=5.0,
        hype_score=0.9,
        funny_score=0.5,
        story_score=0.5,
        reason="",
    )
    return ShortPlan(bucket="custom", clips=(clip,), title="T", vo_prompt="", index=1)


def test_argv_uses_configured_codec_and_opts(monkeypatch):
    monkeypatch.setattr(settings, "ffmpeg_video_codec", "h264_nvenc")
    monkeypatch.setattr(settings, "ffmpeg_video_codec_opts", "-preset p5 -cq 23")
    captured: list[list[str]] = []

    def fake_run(cmd, **_):
        captured.append(cmd)
        return SimpleNamespace(returncode=0, stderr="")

    with patch.object(subprocess, "run", fake_run):
        ok, err = render_short(_plan(), Path("out") / "s.mp4", mode="montage", beat_flash=False)
    assert ok, err
    argv = captured[0]
    i = argv.index("-c:v")
    assert argv[i + 1 : i + 6] == ["h264_nvenc", "-preset", "p5", "-cq", "23"]
    assert "veryfast" not in argv and "libx264" not in argv


def test_no_hardcoded_encoder_literals_in_shorts():
    src = inspect.getsource(shorts)
    assert "veryfast" not in src
    assert "-crf" not in src
