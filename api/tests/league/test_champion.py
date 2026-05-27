"""Sprint L2 — champion identification via Data Dragon template match.

Pure tests on the NCC metric + integration test for /league/detect_champion
with httpx and ffmpeg fully mocked. Real network and real ffmpeg never
run here.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import numpy as np
import pytest

# ----- NCC math -----


def test_ncc_perfect_match_is_one():
    from ai_video_editor.league.candidates.champion import _ncc

    a = np.random.RandomState(0).rand(64, 64).astype(np.float32)
    assert _ncc(a, a) == pytest.approx(1.0, abs=1e-5)


def test_ncc_is_brightness_invariant():
    """NCC subtracts the mean and divides by std — adding a constant
    or scaling positively must not change the score."""
    from ai_video_editor.league.candidates.champion import _ncc

    a = np.random.RandomState(1).rand(64, 64).astype(np.float32)
    b = (a + 50.0) * 1.7  # arbitrary brightness + contrast shift
    assert _ncc(a, b) == pytest.approx(1.0, abs=1e-5)


def test_ncc_zero_variance_returns_zero():
    """A flat image has zero std; we shouldn't divide by zero."""
    from ai_video_editor.league.candidates.champion import _ncc

    flat = np.full((8, 8), 128.0, dtype=np.float32)
    other = np.random.RandomState(2).rand(8, 8).astype(np.float32)
    assert _ncc(flat, other) == 0.0


def test_ncc_picks_correct_pattern_among_candidates():
    """The whole point: NCC vs the right template beats NCC vs others."""
    from ai_video_editor.league.candidates.champion import _ncc

    rng = np.random.RandomState(3)
    target = rng.rand(64, 64).astype(np.float32)
    decoys = [rng.rand(64, 64).astype(np.float32) for _ in range(10)]
    scores = [_ncc(target, d) for d in decoys]
    assert _ncc(target, target) > max(scores)


# ----- detect_champion: end-to-end with all I/O mocked -----


@pytest.fixture(autouse=True)
def _clear_profile_cache():
    from ai_video_editor.profiles import _registry

    _registry.cache_clear()
    yield
    _registry.cache_clear()


def _seed_template_cache(
    workspace: Path, version: str, names_and_arrays: list[tuple[str, np.ndarray]]
):
    cache_dir = workspace / "_cache" / "datadragon" / version / "champions"
    cache_dir.mkdir(parents=True, exist_ok=True)
    for name, arr in names_and_arrays:
        np.save(cache_dir / f"{name}.npy", arr)


def test_detect_champion_returns_best_match_above_threshold(sandbox, monkeypatch):
    """End to end of detect_champion() with the frame-extract mocked and
    a tiny pre-seeded template cache. The frame matches one template
    above the others — that one wins."""
    from ai_video_editor.league.candidates import champion

    version = "14.10.1"
    rng = np.random.RandomState(7)
    ahri = rng.rand(64, 64).astype(np.float32)
    yasuo = rng.rand(64, 64).astype(np.float32)  # decoy template
    _seed_template_cache(sandbox / "ws", version, [("Ahri", ahri), ("Yasuo", yasuo)])

    # The "extracted frame" is literally Ahri's template — NCC will be 1.0.
    monkeypatch.setattr(champion, "_crop_from_recording", lambda *a, **k: ahri.copy())
    monkeypatch.setattr(champion, "latest_version", lambda client=None: version)
    monkeypatch.setattr(
        champion,
        "ensure_templates",
        lambda v, client=None: [
            ("Ahri", sandbox / "ws" / "_cache" / "datadragon" / v / "champions" / "Ahri.npy"),
            ("Yasuo", sandbox / "ws" / "_cache" / "datadragon" / v / "champions" / "Yasuo.npy"),
        ],
    )

    asset = {"id": "a1", "path": str(sandbox / "fake.mp4"), "game": "League of Legends"}
    out = champion.detect_champion(asset, duration=600.0)
    assert out is not None
    assert out["name"] == "Ahri"
    assert out["confidence"] >= 0.95
    assert out["source"] == "cv"
    assert out["datadragon_version"] == version


def test_detect_champion_returns_none_when_no_profile_region(sandbox, monkeypatch):
    """Unknown game → default profile → no champion_portrait region → None."""
    from ai_video_editor.league.candidates import champion

    asset = {"id": "a1", "path": str(sandbox / "fake.mp4"), "game": "Bowling"}
    assert champion.detect_champion(asset, duration=600.0) is None


def test_detect_champion_returns_none_when_below_threshold(sandbox, monkeypatch):
    """If the best NCC is below `min_confidence`, return None (don't lie)."""
    from ai_video_editor.league.candidates import champion

    version = "14.10.1"
    rng = np.random.RandomState(11)
    ahri = rng.rand(64, 64).astype(np.float32)
    # Frame is independent noise — NCC against Ahri will be near zero.
    frame = np.random.RandomState(99).rand(64, 64).astype(np.float32)

    _seed_template_cache(sandbox / "ws", version, [("Ahri", ahri)])
    monkeypatch.setattr(champion, "_crop_from_recording", lambda *a, **k: frame)
    monkeypatch.setattr(champion, "latest_version", lambda client=None: version)
    monkeypatch.setattr(
        champion,
        "ensure_templates",
        lambda v, client=None: [
            ("Ahri", sandbox / "ws" / "_cache" / "datadragon" / v / "champions" / "Ahri.npy")
        ],
    )

    asset = {"id": "a1", "path": str(sandbox / "fake.mp4"), "game": "League of Legends"}
    out = champion.detect_champion(asset, duration=600.0, min_confidence=0.45)
    assert out is None


def test_detect_champion_returns_none_when_frame_extract_fails(sandbox, monkeypatch):
    """ffmpeg crop returns None → detect_champion returns None silently."""
    from ai_video_editor.league.candidates import champion

    monkeypatch.setattr(champion, "_crop_from_recording", lambda *a, **k: None)
    asset = {"id": "a1", "path": str(sandbox / "fake.mp4"), "game": "League of Legends"}
    assert champion.detect_champion(asset, duration=600.0) is None


# ----- /league/detect_champion endpoint -----


def test_detect_champion_endpoint_404s_unknown_asset(client):
    r = client.post(
        "/api/v1/league/detect_champion",
        json={"asset_id": str(uuid.uuid4())},
    )
    assert r.status_code == 404


def test_detect_champion_endpoint_writes_result_file(client, asset, poll, monkeypatch):
    """Job completes successfully and writes the result JSON to the
    expected workspace path; GET /league/champion/{id} reads it back."""
    from ai_video_editor.config import settings
    from ai_video_editor.routers import league as league_router

    # Mock the heavy work: probe + detect both return canned values.
    monkeypatch.setattr(league_router, "_probe_duration", lambda path: 600.0)
    monkeypatch.setattr(
        league_router,
        "detect_champion",
        lambda asset, duration, **kw: {
            "name": "Ahri",
            "confidence": 0.92,
            "source": "cv",
            "datadragon_version": "14.10.1",
            "sample_seconds": 300.0,
        },
    )

    r = client.post(
        "/api/v1/league/detect_champion",
        json={"asset_id": asset["id"]},
    ).json()
    job = poll(client, r["job_id"])
    assert job["status"] == "completed"

    expected = settings.workspace_dir / "champion_detections" / f"{asset['id']}.json"
    assert expected.exists()
    data = json.loads(expected.read_text())
    assert data["name"] == "Ahri"
    assert data["confidence"] == 0.92
    assert data["asset_id"] == asset["id"]

    # GET endpoint reads the same file.
    got = client.get(f"/api/v1/league/champion/{asset['id']}").json()
    assert got["name"] == "Ahri"


def test_detect_champion_endpoint_records_no_match_distinctly(client, asset, poll, monkeypatch):
    """CV running but not locking onto anything is NOT a job failure —
    we want a structured 'no_match' result so callers can distinguish
    it from a crash."""
    from ai_video_editor.routers import league as league_router

    monkeypatch.setattr(league_router, "_probe_duration", lambda path: 600.0)
    monkeypatch.setattr(league_router, "detect_champion", lambda *a, **k: None)

    r = client.post(
        "/api/v1/league/detect_champion",
        json={"asset_id": asset["id"]},
    ).json()
    job = poll(client, r["job_id"])
    assert job["status"] == "completed"

    got = client.get(f"/api/v1/league/champion/{asset['id']}").json()
    assert got["name"] is None
    assert got["reason"] == "no_match"


def test_get_champion_404s_when_no_detection(client, asset):
    """GET /league/champion/{id} returns 404 before any POST has run."""
    r = client.get(f"/api/v1/league/champion/{asset['id']}")
    assert r.status_code == 404


def test_detect_champion_endpoint_failure_records_error(client, asset, poll, monkeypatch):
    """Underlying exception → job failed with the message."""
    from ai_video_editor.routers import league as league_router

    def boom(path):
        raise RuntimeError("ffprobe blew up")

    monkeypatch.setattr(league_router, "_probe_duration", boom)
    r = client.post(
        "/api/v1/league/detect_champion",
        json={"asset_id": asset["id"]},
    ).json()
    job = poll(client, r["job_id"])
    assert job["status"] == "failed"
    assert "ffprobe blew up" in (job.get("error") or "")
