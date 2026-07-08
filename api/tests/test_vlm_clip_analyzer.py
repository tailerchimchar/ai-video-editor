"""Per-clip VLM analyzer tests. Mocks the backend + frame extractor
so the suite stays fast and hermetic."""

from __future__ import annotations

import json
from pathlib import Path

from ai_video_editor.vlm import clip_analyzer as ca
from ai_video_editor.vlm.prompts import ClipAnalysis, game_hints_prompt_version


def _make_clip(tmp_path: Path, name: str = "clip.mp4") -> Path:
    p = tmp_path / name
    p.write_bytes(b"\0" * 1024)
    return p


def _fake_backend(active_model: str = "claude-haiku-4-5"):
    class B:
        name = "anthropic"
        # Attribute lookup — matches AnthropicBackend
        # `.active_model` field.

    b = B()
    b.active_model = active_model
    return b


def test_analyzer_returns_none_when_file_missing() -> None:
    result = ca.analyze_source_clip(
        Path("/does/not/exist.mp4"),
        game=None,
        clip_duration_seconds=30.0,
    )
    assert result is None


def test_analyzer_returns_none_when_disabled(tmp_path, monkeypatch) -> None:
    from ai_video_editor.config import settings

    clip = _make_clip(tmp_path)
    monkeypatch.setattr(settings, "vlm_enabled", False)
    result = ca.analyze_source_clip(
        clip, game="Valorant", clip_duration_seconds=30.0
    )
    assert result is None


def test_analyzer_happy_path(tmp_path, monkeypatch) -> None:
    """Backend returns valid JSON; result is clamped + cached."""
    from ai_video_editor.config import settings

    monkeypatch.setattr(settings, "vlm_enabled", True)
    monkeypatch.setattr(settings, "vlm_clip_analyzer_enabled", True)
    clip = _make_clip(tmp_path)

    # Stub frame extraction so we don't call ffmpeg
    monkeypatch.setattr(
        "ai_video_editor.vlm.clip_analyzer.extract_frames",
        lambda *a, **kw: [tmp_path / "frame_001.jpg"],
    )
    (tmp_path / "frame_001.jpg").write_bytes(b"x")

    # Stub the backend call
    def _fake_call(**_kw):
        return json.dumps(
            {
                "payoff_anchor_seconds": 12.0,
                "buildup_start_seconds": 8.0,
                "resolution_end_seconds": 18.0,
                "payoff_type": "kill",
                "description": "Op one-tap from A main.",
                "confidence": 0.88,
            }
        )

    backend = _fake_backend()
    monkeypatch.setattr(backend, "call", _fake_call, raising=False)
    # ^ B() instance is monkeypatchable

    result = ca.analyze_source_clip(
        clip,
        game="Valorant",
        clip_duration_seconds=30.0,
        backend=backend,
    )
    assert result is not None
    assert result.payoff_anchor_seconds == 12.0
    assert result.buildup_start_seconds == 8.0
    assert result.resolution_end_seconds == 18.0
    assert result.payoff_type == "kill"
    # Sidecar written
    cache = clip.with_suffix(clip.suffix + ".analysis.json")
    assert cache.is_file()
    payload = json.loads(cache.read_text(encoding="utf-8"))
    assert payload["model"] == "claude-haiku-4-5"


def test_analyzer_hits_cache_on_second_call(tmp_path, monkeypatch) -> None:
    """Backend is NOT called when a cached sidecar exists with matching
    prompt_version + model."""
    from ai_video_editor.config import settings

    monkeypatch.setattr(settings, "vlm_enabled", True)
    monkeypatch.setattr(settings, "vlm_clip_analyzer_enabled", True)
    clip = _make_clip(tmp_path)
    cache = clip.with_suffix(clip.suffix + ".analysis.json")

    prompt_version = game_hints_prompt_version("Valorant")
    cache.write_text(
        json.dumps(
            {
                "prompt_version": prompt_version,
                "model": "claude-haiku-4-5",
                "analysis": {
                    "payoff_anchor_seconds": 10.0,
                    "buildup_start_seconds": 5.0,
                    "resolution_end_seconds": 15.0,
                    "payoff_type": "kill",
                    "description": "cached",
                    "confidence": 0.9,
                },
            }
        ),
        encoding="utf-8",
    )

    calls = {"n": 0}

    def _boom(**_kw):
        calls["n"] += 1
        return "{}"

    backend = _fake_backend()
    monkeypatch.setattr(backend, "call", _boom, raising=False)
    monkeypatch.setattr(
        "ai_video_editor.vlm.clip_analyzer.extract_frames",
        lambda *a, **kw: [tmp_path / "frame_001.jpg"],
    )
    result = ca.analyze_source_clip(
        clip,
        game="Valorant",
        clip_duration_seconds=30.0,
        backend=backend,
    )
    assert result is not None
    assert result.description == "cached"
    assert calls["n"] == 0  # backend never called


def test_cache_invalidated_when_prompt_version_bumped(
    tmp_path, monkeypatch
) -> None:
    """A sidecar with a stale `prompt_version` is treated as a miss."""
    from ai_video_editor.config import settings

    monkeypatch.setattr(settings, "vlm_enabled", True)
    monkeypatch.setattr(settings, "vlm_clip_analyzer_enabled", True)
    clip = _make_clip(tmp_path)
    cache = clip.with_suffix(clip.suffix + ".analysis.json")
    cache.write_text(
        json.dumps(
            {
                "prompt_version": -999,  # deliberately mismatched
                "model": "claude-haiku-4-5",
                "analysis": {
                    "payoff_anchor_seconds": 5.0,
                    "buildup_start_seconds": 0.0,
                    "resolution_end_seconds": 10.0,
                    "payoff_type": "kill",
                    "description": "old",
                    "confidence": 0.9,
                },
            }
        ),
        encoding="utf-8",
    )
    called = {"n": 0}

    def _fresh(**_kw):
        called["n"] += 1
        return json.dumps(
            {
                "payoff_anchor_seconds": 12.0,
                "buildup_start_seconds": 8.0,
                "resolution_end_seconds": 18.0,
                "payoff_type": "kill",
                "description": "fresh",
                "confidence": 0.9,
            }
        )

    backend = _fake_backend()
    monkeypatch.setattr(backend, "call", _fresh, raising=False)
    monkeypatch.setattr(
        "ai_video_editor.vlm.clip_analyzer.extract_frames",
        lambda *a, **kw: [tmp_path / "frame_001.jpg"],
    )
    (tmp_path / "frame_001.jpg").write_bytes(b"x")

    result = ca.analyze_source_clip(
        clip,
        game="Valorant",
        clip_duration_seconds=30.0,
        backend=backend,
    )
    assert called["n"] == 1
    assert result is not None
    assert result.description == "fresh"


def test_analyzer_clamps_out_of_range_values(tmp_path, monkeypatch) -> None:
    """VLM sometimes returns end > duration; clamp instead of reject."""
    from ai_video_editor.config import settings

    monkeypatch.setattr(settings, "vlm_enabled", True)
    monkeypatch.setattr(settings, "vlm_clip_analyzer_enabled", True)
    monkeypatch.setattr(
        settings, "vlm_clip_analyzer_cache_enabled", False
    )
    clip = _make_clip(tmp_path)
    monkeypatch.setattr(
        "ai_video_editor.vlm.clip_analyzer.extract_frames",
        lambda *a, **kw: [tmp_path / "frame_001.jpg"],
    )
    (tmp_path / "frame_001.jpg").write_bytes(b"x")

    def _wild(**_kw):
        return json.dumps(
            {
                "payoff_anchor_seconds": 45.0,   # beyond duration=30
                "buildup_start_seconds": 25.0,
                "resolution_end_seconds": 100.0, # way beyond
                "payoff_type": "kill",
                "description": "clamped",
                "confidence": 0.9,
            }
        )

    backend = _fake_backend()
    monkeypatch.setattr(backend, "call", _wild, raising=False)

    result = ca.analyze_source_clip(
        clip,
        game="Valorant",
        clip_duration_seconds=30.0,
        backend=backend,
    )
    assert result is not None
    assert result.payoff_anchor_seconds <= 30.0
    assert result.resolution_end_seconds <= 30.0
    assert result.buildup_start_seconds <= result.payoff_anchor_seconds


def test_analyzer_returns_none_on_parse_failure(tmp_path, monkeypatch) -> None:
    """Backend returns garbage → analyzer returns None (never raises)."""
    from ai_video_editor.config import settings

    monkeypatch.setattr(settings, "vlm_enabled", True)
    monkeypatch.setattr(settings, "vlm_clip_analyzer_enabled", True)
    monkeypatch.setattr(
        settings, "vlm_clip_analyzer_cache_enabled", False
    )
    clip = _make_clip(tmp_path)
    monkeypatch.setattr(
        "ai_video_editor.vlm.clip_analyzer.extract_frames",
        lambda *a, **kw: [tmp_path / "frame_001.jpg"],
    )
    (tmp_path / "frame_001.jpg").write_bytes(b"x")

    def _garbage(**_kw):
        return "not json at all"

    backend = _fake_backend()
    monkeypatch.setattr(backend, "call", _garbage, raising=False)

    result = ca.analyze_source_clip(
        clip,
        game="Valorant",
        clip_duration_seconds=30.0,
        backend=backend,
    )
    assert result is None


def test_clamp_helper_handles_degenerate_window() -> None:
    """When start > end, the clamp helper reconstructs a 5s window
    centered on the anchor."""
    analysis = ClipAnalysis(
        payoff_anchor_seconds=10.0,
        buildup_start_seconds=15.0,       # invalid: after anchor
        resolution_end_seconds=8.0,       # invalid: before anchor
        payoff_type="kill",
        description="broken",
        confidence=0.5,
    )
    fixed = ca._clamp(analysis, clip_duration=30.0)
    assert fixed.buildup_start_seconds <= fixed.payoff_anchor_seconds
    assert fixed.payoff_anchor_seconds <= fixed.resolution_end_seconds
