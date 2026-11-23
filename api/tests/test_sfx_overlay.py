"""Tests for POST /sfx/overlay -- one-shot SFX layering on any .mp4."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_video_editor.config import settings
from ai_video_editor.edits import apply_sfx
from ai_video_editor.main import app


def _make_dummy_mp4(path: Path, seconds: int = 5) -> None:
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
            f"color=c=black:s=64x64:r=10:d={seconds}",
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


def _make_dummy_wav(path: Path, seconds: float = 0.3) -> None:
    """Short tone -- fine as a fake vine-boom for tests."""
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
            f"sine=frequency=100:duration={seconds}",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def client(monkeypatch, tmp_path) -> TestClient:
    monkeypatch.setattr(settings, "workspace_dir", tmp_path)
    return TestClient(app)


def test_sfx_overlay_writes_output(client, tmp_path) -> None:
    src = tmp_path / "src.mp4"
    _make_dummy_mp4(src)
    sfx = tmp_path / "boom.wav"
    _make_dummy_wav(sfx)

    resp = client.post(
        "/api/v1/sfx/overlay",
        json={
            "source_path": str(src),
            "sfx": [{"sfx_path": str(sfx), "at_seconds": 1.0, "volume": 1.5}],
            "name": "test_boom",
        },
    )
    assert resp.status_code == 200
    out = Path(resp.json()["output_path"])
    assert out.is_file()
    assert out.parent == settings.workspace_dir / "sfx_overlays"


def test_sfx_overlay_supports_multiple_hits(client, tmp_path) -> None:
    """Two SFX at different timestamps + volumes -> both mixed in."""
    src = tmp_path / "src.mp4"
    _make_dummy_mp4(src)
    sfx1 = tmp_path / "s1.wav"
    sfx2 = tmp_path / "s2.wav"
    _make_dummy_wav(sfx1)
    _make_dummy_wav(sfx2)

    resp = client.post(
        "/api/v1/sfx/overlay",
        json={
            "source_path": str(src),
            "sfx": [
                {"sfx_path": str(sfx1), "at_seconds": 0.5, "volume": 1.0},
                {"sfx_path": str(sfx2), "at_seconds": 2.5, "volume": 0.8},
            ],
        },
    )
    assert resp.status_code == 200
    assert Path(resp.json()["output_path"]).is_file()


def test_sfx_overlay_rejects_missing_source(client, tmp_path) -> None:
    resp = client.post(
        "/api/v1/sfx/overlay",
        json={
            "source_path": str(tmp_path / "nope.mp4"),
            "sfx": [{"sfx_path": "x", "at_seconds": 0}],
        },
    )
    assert resp.status_code == 404


def test_sfx_overlay_fails_when_all_sfx_paths_missing(client, tmp_path) -> None:
    """Every sfx_path is bogus -- the encoder returns 500 with a
    meaningful error, not a silent success."""
    src = tmp_path / "src.mp4"
    _make_dummy_mp4(src)
    resp = client.post(
        "/api/v1/sfx/overlay",
        json={
            "source_path": str(src),
            "sfx": [{"sfx_path": str(tmp_path / "nope.wav"), "at_seconds": 1.0}],
        },
    )
    assert resp.status_code == 500
    assert "no valid sfx entries" in resp.json()["detail"]


def test_apply_sfx_pure_filter_shape(tmp_path) -> None:
    """Directly-called primitive: verify it builds a valid ffmpeg
    filter chain by asserting output shape."""
    src = tmp_path / "src.mp4"
    _make_dummy_mp4(src)
    sfx = tmp_path / "boom.wav"
    _make_dummy_wav(sfx)
    out = tmp_path / "out.mp4"

    ok, err = apply_sfx(
        str(src),
        str(out),
        sfx=[
            {"sfx_path": str(sfx), "at_seconds": 1.0, "volume": 1.0},
            {"sfx_path": str(sfx), "at_seconds": 2.0, "volume": 0.5},
        ],
    )
    assert ok is True, err
    assert out.is_file()


def test_apply_sfx_mix_keeps_the_source_length(tmp_path, monkeypatch) -> None:
    """A kaching near the end must not stretch the audio past the video."""
    from ai_video_editor import edits

    sfx = tmp_path / "boom.wav"
    sfx.write_bytes(b"x")
    seen = []
    monkeypatch.setattr(edits, "_run_ffmpeg", lambda cmd: seen.append(cmd) or (True, None))
    assert apply_sfx("src.mp4", str(tmp_path / "out.mp4"),
                     sfx=[{"sfx_path": str(sfx), "at_seconds": 9.5}]) == (True, None)  # fmt: skip
    graph = seen[0][seen[0].index("-filter_complex") + 1]
    assert "amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]" in graph
