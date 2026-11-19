"""Tests for the kill-anchor HTTP endpoints -- trim snap, preview,
auto-cut. Mocks the Overwolf lookup so the tests don't need a real
Outplayed log on disk."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_video_editor.candidates.overwolf_events import EventAnchor
from ai_video_editor.config import settings
from ai_video_editor.main import app


def _make_dummy_mp4(path: Path, seconds: int = 30) -> None:
    subprocess.run(
        [
            settings.ffmpeg_path, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", f"color=c=black:s=64x64:r=10:d={seconds}",
            "-f", "lavfi", "-i", f"anullsrc=r=8000:cl=mono:d={seconds}",
            "-shortest", "-c:v", "libx264", "-c:a", "aac",
            str(path),
        ],
        check=True, capture_output=True,
    )


@pytest.fixture
def client(monkeypatch, tmp_path) -> TestClient:
    monkeypatch.setattr(settings, "workspace_dir", tmp_path)
    return TestClient(app)


@pytest.fixture
def src_mp4(tmp_path) -> Path:
    p = tmp_path / "src.mp4"
    _make_dummy_mp4(p)
    return p


def test_list_kill_anchors_returns_anchors(monkeypatch, client, src_mp4) -> None:
    def fake_anchors(_path, _duration, *, include_clusters=True):
        return [
            EventAnchor(10.0, "kill", {"anchor_seconds": 10.0, "headshot": True}),
            EventAnchor(15.0, "kill", {"anchor_seconds": 15.0}),
            EventAnchor(12.5, "multi_kill_2", {"anchor_seconds": 12.5, "cluster_size": 2}),
        ]
    monkeypatch.setattr(
        "ai_video_editor.routers.clips.kill_anchors_for_video", fake_anchors
    )

    resp = client.post(
        "/api/v1/clips/list_kill_anchors",
        json={"source_path": str(src_mp4)},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 3
    assert body["anchors"][0]["event_type"] == "kill"
    assert body["anchors"][0]["headshot"] is True


def test_list_kill_anchors_empty_when_no_log(monkeypatch, client, src_mp4) -> None:
    monkeypatch.setattr(
        "ai_video_editor.routers.clips.kill_anchors_for_video",
        lambda *a, **kw: [],
    )
    resp = client.post(
        "/api/v1/clips/list_kill_anchors",
        json={"source_path": str(src_mp4)},
    )
    assert resp.status_code == 200
    assert resp.json()["count"] == 0


def test_list_kill_anchors_404_on_missing_file(client, tmp_path) -> None:
    resp = client.post(
        "/api/v1/clips/list_kill_anchors",
        json={"source_path": str(tmp_path / "nope.mp4")},
    )
    assert resp.status_code == 404


def test_trim_source_anchor_to_kill_snaps_window(
    monkeypatch, client, src_mp4
) -> None:
    monkeypatch.setattr(
        "ai_video_editor.routers.clips.kill_anchors_for_video",
        lambda *a, **kw: [
            EventAnchor(11.0, "kill", {"anchor_seconds": 11.0})
        ],
    )

    resp = client.post(
        "/api/v1/clips/trim_source",
        json={
            "source_path": str(src_mp4),
            "start_seconds": 5.0,
            "end_seconds": 15.0,
            "anchor_to_kill": True,
            "name": "snapped_kill",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["snapped"] is True
    assert body["anchor_seconds"] == 11.0
    assert body["event_type"] == "kill"
    assert body["start_seconds"] == 8.0
    assert body["end_seconds"] == 13.5


def test_trim_source_anchor_to_kill_falls_through_when_no_anchor(
    monkeypatch, client, src_mp4
) -> None:
    monkeypatch.setattr(
        "ai_video_editor.routers.clips.kill_anchors_for_video",
        lambda *a, **kw: [],
    )

    resp = client.post(
        "/api/v1/clips/trim_source",
        json={
            "source_path": str(src_mp4),
            "start_seconds": 5.0,
            "end_seconds": 15.0,
            "anchor_to_kill": True,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["snapped"] is False
    assert body["anchor_seconds"] is None
    assert body["start_seconds"] == 5.0
    assert body["end_seconds"] == 15.0


def test_trim_source_without_anchor_flag_untouched(
    monkeypatch, client, src_mp4
) -> None:
    # anchor_to_kill defaults False -> kill_anchors_for_video should NOT
    # be called (defensive: sentinel that would raise if called).
    def _raise(*a, **kw):
        raise AssertionError("should not be called when anchor_to_kill=False")

    monkeypatch.setattr(
        "ai_video_editor.routers.clips.kill_anchors_for_video", _raise
    )

    resp = client.post(
        "/api/v1/clips/trim_source",
        json={
            "source_path": str(src_mp4),
            "start_seconds": 5.0,
            "end_seconds": 15.0,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["snapped"] is False


def test_auto_cut_kills_writes_one_clip_per_anchor(
    monkeypatch, client, src_mp4
) -> None:
    monkeypatch.setattr(
        "ai_video_editor.routers.clips.kill_anchors_for_video",
        lambda *a, **kw: [
            EventAnchor(8.0, "kill", {"anchor_seconds": 8.0}),
            EventAnchor(20.0, "kill", {"anchor_seconds": 20.0}),
        ],
    )
    resp = client.post(
        "/api/v1/clips/auto_cut_kills",
        json={"source_path": str(src_mp4), "name_prefix": "test"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 2
    for clip in body["clips"]:
        assert Path(clip["output_path"]).is_file()
        assert clip["event_type"] == "kill"


def test_auto_cut_kills_empty_when_log_missing(
    monkeypatch, client, src_mp4
) -> None:
    monkeypatch.setattr(
        "ai_video_editor.routers.clips.kill_anchors_for_video",
        lambda *a, **kw: [],
    )
    resp = client.post(
        "/api/v1/clips/auto_cut_kills",
        json={"source_path": str(src_mp4)},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["clips"] == []
    assert "reason" in body


def test_auto_cut_kills_merges_overlapping(monkeypatch, client, src_mp4) -> None:
    # Two kills 3s apart -> single-kill windows overlap -> one merged clip.
    monkeypatch.setattr(
        "ai_video_editor.routers.clips.kill_anchors_for_video",
        lambda *a, **kw: [
            EventAnchor(10.0, "kill", {"anchor_seconds": 10.0}),
            EventAnchor(13.0, "kill", {"anchor_seconds": 13.0}),
        ],
    )
    resp = client.post(
        "/api/v1/clips/auto_cut_kills",
        json={"source_path": str(src_mp4)},
    )
    assert resp.status_code == 200
    assert resp.json()["count"] == 1
