"""Tests for POST /clips/trim_source.

Real ffmpeg is required to trim -- we use a tiny lavfi-generated mp4
so tests stay under 1s and don't depend on user footage.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_video_editor.config import settings
from ai_video_editor.main import app


def _make_dummy_mp4(path: Path, seconds: int = 5) -> None:
    """Generate a tiny synthetic mp4 via ffmpeg lavfi. Avoids checking
    a fixture into the repo."""
    cmd = [
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
    ]
    subprocess.run(cmd, check=True, capture_output=True)


@pytest.fixture
def client(monkeypatch, tmp_path) -> TestClient:
    monkeypatch.setattr(settings, "workspace_dir", tmp_path)
    return TestClient(app)


def test_trim_source_writes_output_to_trims_dir(client, tmp_path) -> None:
    src = tmp_path / "src.mp4"
    _make_dummy_mp4(src, seconds=5)

    resp = client.post(
        "/api/v1/clips/trim_source",
        json={
            "source_path": str(src),
            "start_seconds": 1.0,
            "end_seconds": 3.0,
            "name": "test_trim",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    out = Path(body["output_path"])
    assert out.is_file()
    assert out.parent == settings.workspace_dir / "trims"
    assert out.name == "test_trim.mp4"


def test_trim_source_auto_names_when_name_omitted(client, tmp_path) -> None:
    src = tmp_path / "myvod.mp4"
    _make_dummy_mp4(src, seconds=5)

    resp = client.post(
        "/api/v1/clips/trim_source",
        json={"source_path": str(src), "start_seconds": 1.0, "end_seconds": 3.0},
    )
    assert resp.status_code == 200
    out_name = Path(resp.json()["output_path"]).name
    assert out_name == "myvod_1-3s.mp4"


def test_trim_source_rejects_missing_source(client, tmp_path) -> None:
    resp = client.post(
        "/api/v1/clips/trim_source",
        json={
            "source_path": str(tmp_path / "nope.mp4"),
            "start_seconds": 0.0,
            "end_seconds": 1.0,
        },
    )
    assert resp.status_code == 404


def test_trim_source_rejects_reversed_range(client, tmp_path) -> None:
    src = tmp_path / "src.mp4"
    _make_dummy_mp4(src, seconds=5)

    resp = client.post(
        "/api/v1/clips/trim_source",
        json={
            "source_path": str(src),
            "start_seconds": 3.0,
            "end_seconds": 1.0,
        },
    )
    assert resp.status_code == 400


def test_trim_source_sanitizes_name_special_chars(client, tmp_path) -> None:
    src = tmp_path / "src.mp4"
    _make_dummy_mp4(src, seconds=5)

    resp = client.post(
        "/api/v1/clips/trim_source",
        json={
            "source_path": str(src),
            "start_seconds": 0.5,
            "end_seconds": 2.0,
            "name": "why you always: stack on ecos!",
        },
    )
    assert resp.status_code == 200
    name = Path(resp.json()["output_path"]).name
    # Colons, spaces, exclamation collapse to underscores; no path-traversal
    assert name == "why_you_always_stack_on_ecos.mp4"
