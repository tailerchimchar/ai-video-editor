"""Narration HTTP API (routers/narration.py): HTTP only, everything behind
injectable dependencies (target registry, store, exporter)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from ai_video_editor.main import app
from ai_video_editor.narration import targets
from ai_video_editor.narration.models import TargetRef
from ai_video_editor.routers import narration as narration_router

BASE = "/api/v1/narration/fake/r1"
WEBM = {"Content-Type": "video/webm"}


class FakeTarget:
    kind = "fake"

    def __init__(self, root: Path) -> None:
        self.root = root

    def work_dir(self, key: str) -> Path:
        return self.root / key

    def video_path(self, key: str) -> Path:
        path = self.work_dir(key) / "video.mp4"
        if not path.is_file():
            raise targets.UnknownTargetError(key)
        return path

    def game(self, key: str) -> str:
        return "league"

    def video_url(self, key: str) -> str:
        return f"/workspace/fake/{key}/video.mp4"

    def list_keys(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(p.name for p in self.root.iterdir() if (p / "video.mp4").is_file())


@pytest.fixture
def video(sandbox, monkeypatch) -> Path:
    target = FakeTarget(sandbox / "fake")
    monkeypatch.setitem(targets._REGISTRY, "fake", target)
    path = target.work_dir("r1") / "video.mp4"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"v1")
    return path


@pytest.fixture
def exporter():
    """A fake exporter swapped in through the dependency."""
    calls: list[dict] = []

    def fake(ref, *, target, store, force=False):
        calls.append({"ref": ref, "force": force})
        out = store.export_path(ref)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"x")
        return out

    app.dependency_overrides[narration_router.get_exporter] = lambda: fake
    yield calls
    app.dependency_overrides.pop(narration_router.get_exporter, None)


def _take(client, start=1.5, duration=2.0, cam=False, body=b"abc", headers=WEBM):
    return client.post(
        f"{BASE}/takes?start_s={start}&duration_s={duration}&has_camera={str(cam).lower()}",
        content=body,
        headers=headers,
    )


# -- session -----------------------------------------------------------
def test_unknown_kind_404(client, video):
    assert client.get("/api/v1/narration/nope/r1").status_code == 404


def test_bad_key_422_and_missing_video_404(client, video):
    assert client.get("/api/v1/narration/fake/bad.key").status_code == 422
    assert client.get("/api/v1/narration/fake/other").status_code == 404


def test_first_get_creates_session(client, video):
    r = client.get(BASE)
    assert r.status_code == 200
    body = r.json()
    assert body["session"]["target"] == {"kind": "fake", "key": "r1"}
    assert body["session"]["takes"] == []
    assert body["export_url"] is None
    assert body["video_url"] == "/workspace/fake/r1/video.mp4"
    assert body["game"] == "league"
    assert body["stale"] is False
    assert body["session"]["video_fingerprint"]
    assert set(body["camera_px"]) == {"x", "y", "w", "h"}
    assert body["default_camera_px"] == body["camera_px"]  # no owner override yet
    assert (video.parent / "narration" / "session.json").is_file()


# -- takes -------------------------------------------------------------
def test_upload_take(client, video):
    r = _take(client, cam=True)
    assert r.status_code == 200
    take = r.json()
    assert len(take["id"]) == 12
    assert (take["start_s"], take["duration_s"], take["has_camera"]) == (1.5, 2.0, True)
    assert take["media"] == f"{take['id']}.webm"
    saved = video.parent / "narration" / "takes" / take["media"]
    assert saved.read_bytes() == b"abc"
    assert [t["id"] for t in client.get(BASE).json()["session"]["takes"]] == [take["id"]]


def test_upload_accepts_audio_webm_with_codecs(client, video):
    r = _take(client, headers={"Content-Type": "audio/webm;codecs=opus"})
    assert r.status_code == 200


def test_upload_wrong_content_type_415(client, video):
    assert _take(client, headers={"Content-Type": "video/mp4"}).status_code == 415
    assert not (video.parent / "narration" / "takes").exists()


def test_upload_too_large_413(client, video, monkeypatch):
    monkeypatch.setattr(narration_router, "MAX_UPLOAD_BYTES", 4)
    assert _take(client, body=b"12345").status_code == 413
    assert _take(client, body=b"1234").status_code == 200


def test_upload_bad_params_422(client, video):
    assert _take(client, duration=0).status_code == 422
    assert _take(client, start=-1).status_code == 422


def test_delete_take(client, video):
    take = _take(client).json()
    r = client.delete(f"{BASE}/takes/{take['id']}")
    assert r.status_code == 200
    assert r.json()["session"]["takes"] == []
    assert not (video.parent / "narration" / "takes" / take["media"]).exists()
    assert client.delete(f"{BASE}/takes/{take['id']}").status_code == 404


# -- camera / stale / relock -------------------------------------------
def test_put_camera(client, video):
    r = client.put(f"{BASE}/camera", json={"enabled": True, "width_frac": 0.25, "x": 0.5, "y": 0.5})
    assert r.status_code == 200
    body = r.json()
    assert body["session"]["camera"]["enabled"] is True
    assert body["camera_px"]["w"] == 480
    assert body["camera_px"] != body["default_camera_px"]
    assert client.get(BASE).json()["session"]["camera"]["width_frac"] == 0.25
    assert client.put(f"{BASE}/camera", json={"width_frac": 0.9}).status_code == 422


def test_stale_after_video_changes_and_relock_clears(client, video):
    _take(client)
    video.write_bytes(b"a re-rendered video")
    assert client.get(BASE).json()["stale"] is True
    r = client.post(f"{BASE}/relock")
    assert r.status_code == 200 and r.json()["stale"] is False
    assert client.get(BASE).json()["stale"] is False


def test_no_takes_is_never_stale_and_first_take_pins_the_video(client, video):
    client.get(BASE)
    video.write_bytes(b"a re-rendered video")
    assert client.get(BASE).json()["stale"] is False
    _take(client)  # recorded over the new video
    assert client.get(BASE).json()["stale"] is False


def test_view_sends_the_narration_folder_url(client, video):
    assert client.get(BASE).json()["narration_url"].endswith("/narration")


# -- export ------------------------------------------------------------
def _wait(client, job_id):
    for _ in range(100):
        job = client.get(f"/api/v1/jobs/{job_id}").json()
        if job["status"] in ("completed", "failed"):
            return job
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_export_creates_job(client, video, exporter):
    _take(client)
    r = client.post(f"{BASE}/export", json={})
    assert r.status_code == 200
    job = _wait(client, r.json()["job_id"])
    assert job["status"] == "completed"
    assert job["output_path"].endswith("narrated.mp4")
    assert client.get(BASE).json()["export_url"] == "/workspace/fake/r1/narration/narrated.mp4"
    assert exporter == [{"ref": TargetRef(kind="fake", key="r1"), "force": False}]


def test_export_stale_409_without_force_and_no_job(client, video, exporter):
    _take(client)
    video.write_bytes(b"changed")
    assert client.post(f"{BASE}/export", json={"force": False}).status_code == 409
    assert exporter == []
    r = client.post(f"{BASE}/export", json={"force": True})
    assert r.status_code == 200
    _wait(client, r.json()["job_id"])
    assert exporter[0]["force"] is True


def test_export_without_takes_422(client, video, exporter):
    assert client.post(f"{BASE}/export", json={}).status_code == 422


def test_export_failure_marks_job_failed(client, video):
    _take(client)

    def boom(ref, *, target, store, force=False):
        raise RuntimeError("ffmpeg exploded")

    app.dependency_overrides[narration_router.get_exporter] = lambda: boom
    try:
        job = _wait(client, client.post(f"{BASE}/export", json={}).json()["job_id"])
    finally:
        app.dependency_overrides.pop(narration_router.get_exporter, None)
    assert job["status"] == "failed" and "ffmpeg exploded" in job["error"]


def test_store_is_swappable(client, video):
    """A different store (future per-user) plugs in via the dependency."""
    from ai_video_editor.narration.store import FileNarrationStore

    other = video.parent.parent / "elsewhere"
    app.dependency_overrides[narration_router.get_store] = lambda: FileNarrationStore(
        lambda ref: other
    )
    try:
        client.get(BASE)
    finally:
        app.dependency_overrides.pop(narration_router.get_store, None)
    assert (other / "narration" / "session.json").is_file()
    assert not (video.parent / "narration").exists()


# -- frames / peaks / markers -------------------------------------------
@pytest.fixture
def renderers():
    from ai_video_editor.narration import strips
    from ai_video_editor.narration.models import Peaks

    def frames(video, cache_dir, fp):
        return strips.FrameStripFiles(
            sprite=cache_dir / f"frames-{fp}.jpg",
            meta=cache_dir / f"frames-{fp}.json",
            every_s=4.0,
            tile_w=160,
            tile_h=90,
            cols=199,
            count=199,
        )

    app.dependency_overrides[narration_router.get_frames_renderer] = lambda: frames
    app.dependency_overrides[narration_router.get_peaks_renderer] = lambda: (
        lambda video, cache_dir, fp: Peaks(rate_hz=10, peaks=[0.0, 1.0])
    )
    yield
    app.dependency_overrides.pop(narration_router.get_frames_renderer, None)
    app.dependency_overrides.pop(narration_router.get_peaks_renderer, None)


def test_frames_route(client, video, renderers):
    body = client.get(f"{BASE}/frames").json()
    assert body["count"] == 199 and body["tile_h"] == 90 and body["every_s"] == 4.0
    fp = targets.fingerprint(video)
    assert body["sprite_url"].endswith(f"cache/frames-{fp}.jpg")


def test_peaks_route(client, video, renderers):
    assert client.get(f"{BASE}/peaks").json() == {"rate_hz": 10, "peaks": [0.0, 1.0]}


def test_markers_route_reads_sorted_cues(client, video):
    import json

    cues = video.parent / "narration" / "cues.json"
    cues.parent.mkdir(parents=True)
    cues.write_text(
        json.dumps([{"at_s": 20, "label": "b"}, {"at_s": 13.8, "label": "X1 opening"}]),
        encoding="utf-8",
    )
    got = client.get(f"{BASE}/markers").json()
    assert [m["at_s"] for m in got] == [13.8, 20.0]
    assert got[0] == {"at_s": 13.8, "label": "X1 opening", "kind": "cue"}


def test_markers_route_empty_without_cues_file(client, video):
    assert client.get(f"{BASE}/markers").json() == []


def test_load_cues_invalid_and_truncated(video):
    from ai_video_editor.narration import service
    from ai_video_editor.narration.store import FileNarrationStore

    store = FileNarrationStore(lambda ref: video.parent)
    ref = TargetRef(kind="fake", key="r1")
    path = store.cues_path(ref)
    path.parent.mkdir(parents=True)
    path.write_text("not json")
    assert service.load_cues(store, ref) == []
    path.write_text('[{"at_s": 1, "label": "' + "x" * 200 + '"}]')
    assert len(service.load_cues(store, ref)[0].label) == 80
