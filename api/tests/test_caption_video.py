"""Tests for POST /clips/caption_video -- raw-file drawtext overlay
with per-segment font support."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_video_editor.config import settings
from ai_video_editor.main import app


def _make_dummy_mp4(path: Path, seconds: int = 3) -> None:
    subprocess.run(
        [
            settings.ffmpeg_path,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c=black:s=128x128:r=10:d={seconds}",
            "-f",
            "lavfi",
            "-i",
            f"anullsrc=r=8000:cl=mono:d={seconds}",
            "-shortest",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def client(monkeypatch, tmp_path) -> TestClient:
    monkeypatch.setattr(settings, "workspace_dir", tmp_path)
    return TestClient(app)


def test_caption_video_writes_output_to_captions_dir(client, tmp_path) -> None:
    src = tmp_path / "src.mp4"
    _make_dummy_mp4(src)
    resp = client.post(
        "/api/v1/clips/caption_video",
        json={
            "source_path": str(src),
            "segments": [
                {"text": "TEST", "start_seconds": 0.0, "end_seconds": 2.0},
            ],
            "name": "cap_test",
            "aspect": "source",
        },
    )
    assert resp.status_code == 200
    out = Path(resp.json()["output_path"])
    assert out.is_file()
    assert out.parent == settings.workspace_dir / "captions"


def test_caption_video_accepts_per_segment_font_path(client, tmp_path) -> None:
    """A segment with a font_path uses that font; the filter chain
    should still succeed end-to-end."""
    src = tmp_path / "src.mp4"
    _make_dummy_mp4(src)
    resp = client.post(
        "/api/v1/clips/caption_video",
        json={
            "source_path": str(src),
            "segments": [
                {
                    "text": "hello",
                    "start_seconds": 0.0,
                    "end_seconds": 1.0,
                    "font_path": settings.caption_font_path,
                },
                {
                    "text": "world",
                    "start_seconds": 1.0,
                    "end_seconds": 2.0,
                    "font_path": settings.caption_emoji_font_path,
                },
            ],
            "aspect": "source",
        },
    )
    assert resp.status_code == 200
    assert Path(resp.json()["output_path"]).is_file()


def test_caption_video_rejects_missing_source(client, tmp_path) -> None:
    resp = client.post(
        "/api/v1/clips/caption_video",
        json={
            "source_path": str(tmp_path / "nope.mp4"),
            "segments": [{"text": "x", "start_seconds": 0.0, "end_seconds": 1.0}],
        },
    )
    assert resp.status_code == 404


def test_caption_video_requires_at_least_one_segment(client, tmp_path) -> None:
    src = tmp_path / "src.mp4"
    _make_dummy_mp4(src)
    resp = client.post(
        "/api/v1/clips/caption_video",
        json={"source_path": str(src), "segments": []},
    )
    assert resp.status_code == 422  # pydantic min_length=1


def test_caption_filters_uses_default_font_when_segment_omits_it() -> None:
    """Pure filter test -- no HTTP -- verify the default font is used
    when a segment doesn't specify one."""
    from ai_video_editor.edits import caption_filters

    chain = caption_filters(
        [{"text": "hi", "start_seconds": 0.0, "end_seconds": 1.0}],
        clip_start_offset=0.0,
    )
    # Default caption_font_path is used (arial.ttf on Windows).
    assert "arial" in chain.lower()


def test_caption_filters_uses_segment_font_when_present() -> None:
    from ai_video_editor.edits import caption_filters

    chain = caption_filters(
        [
            {
                "text": "hi",
                "start_seconds": 0.0,
                "end_seconds": 1.0,
                "font_path": "C:/Windows/Fonts/seguiemj.ttf",
            }
        ],
        clip_start_offset=0.0,
    )
    assert "seguiemj" in chain.lower()
