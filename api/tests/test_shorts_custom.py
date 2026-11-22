"""Custom-compile + browse-tool tests. Pure filesystem — no ffmpeg."""

from __future__ import annotations

import json
from pathlib import Path

from ai_video_editor.shorts import (
    list_custom_shorts,
    list_highlights_clips,
    list_highlights_sessions,
    list_outplayed_clips,
)


def _make_mp4(path: Path, size: int = 1024) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"\0" * size)


# ---------------------------------------------------------------------
# Browse — Outplayed
# ---------------------------------------------------------------------


def test_list_outplayed_clips_empty(tmp_path, monkeypatch) -> None:
    from ai_video_editor.config import settings

    monkeypatch.setattr(settings, "outplayed_media_dir", tmp_path)
    result = list_outplayed_clips("Nonexistent")
    assert result["ok"] is True
    assert result["clips_total"] == 0
    assert result["clips"] == []


def test_list_outplayed_clips_returns_full_list(tmp_path, monkeypatch) -> None:
    from ai_video_editor.config import settings

    monkeypatch.setattr(settings, "outplayed_media_dir", tmp_path)
    game_dir = tmp_path / "Valorant" / "session_1"
    _make_mp4(game_dir / "a.mp4")
    _make_mp4(game_dir / "b.mp4")

    monkeypatch.setattr(
        "ai_video_editor.candidates.probe.get_duration_seconds",
        lambda p: 30.0,
    )
    result = list_outplayed_clips("Valorant")
    assert result["clips_total"] == 2
    assert result["clips_returned"] == 2
    # Path + session info surfaced
    for entry in result["clips"]:
        assert entry["path"].endswith(".mp4")
        assert entry["session"] == "session_1"
        assert entry["duration_seconds"] == 30.0


def test_list_outplayed_clips_respects_limit(tmp_path, monkeypatch) -> None:
    from ai_video_editor.config import settings

    monkeypatch.setattr(settings, "outplayed_media_dir", tmp_path)
    game_dir = tmp_path / "Valorant" / "session_1"
    for i in range(5):
        _make_mp4(game_dir / f"clip_{i}.mp4")

    monkeypatch.setattr(
        "ai_video_editor.candidates.probe.get_duration_seconds",
        lambda p: 30.0,
    )
    result = list_outplayed_clips("Valorant", limit=2)
    assert result["clips_total"] == 5
    assert result["clips_returned"] == 2


# ---------------------------------------------------------------------
# Browse — highlights sessions
# ---------------------------------------------------------------------


def test_list_highlights_sessions_no_workspace(tmp_path, monkeypatch) -> None:
    from ai_video_editor.config import settings

    monkeypatch.setattr(settings, "workspace_dir", tmp_path)
    # No highlights folder yet
    result = list_highlights_sessions()
    assert result == {"games": []}


def test_list_highlights_sessions_multi_game(tmp_path, monkeypatch) -> None:
    from ai_video_editor.config import settings

    monkeypatch.setattr(settings, "workspace_dir", tmp_path)
    root = tmp_path / "highlights"
    (root / "League of Legends" / "07-04-2026_15h15").mkdir(parents=True)
    (root / "League of Legends" / "07-04-2026_15h15" / "index.json").write_text(
        "{}", encoding="utf-8"
    )
    (root / "Valorant" / "07-07-2026_20h00").mkdir(parents=True)

    result = list_highlights_sessions()
    game_names = {g["game"] for g in result["games"]}
    assert game_names == {"League of Legends", "Valorant"}
    for g in result["games"]:
        assert g["session_count"] >= 1


def test_list_highlights_sessions_filters_by_game(tmp_path, monkeypatch) -> None:
    from ai_video_editor.config import settings

    monkeypatch.setattr(settings, "workspace_dir", tmp_path)
    root = tmp_path / "highlights"
    (root / "League of Legends" / "07-04-2026_15h15").mkdir(parents=True)
    (root / "Valorant" / "07-07-2026_20h00").mkdir(parents=True)

    result = list_highlights_sessions("Valorant")
    assert len(result["games"]) == 1
    assert result["games"][0]["game"] == "Valorant"


# ---------------------------------------------------------------------
# Browse — clips inside a highlights session
# ---------------------------------------------------------------------


def _write_highlights_index(folder: Path, clips: list[dict]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "index.json").write_text(json.dumps({"clips": clips}), encoding="utf-8")
    for c in clips:
        (folder / c["file"]).write_bytes(b"")


def test_list_highlights_clips_returns_index_json(tmp_path, monkeypatch) -> None:
    from ai_video_editor.config import settings

    monkeypatch.setattr(settings, "workspace_dir", tmp_path)
    folder = tmp_path / "highlights" / "Valorant" / "07-07-2026_10h00"
    _write_highlights_index(
        folder,
        [
            {
                "file": "01_kill.mp4",
                "event": "kill",
                "start_seconds": 100,
                "end_seconds": 110,
                "hype_score": 0.9,
                "reason": "ace",
                "ok": True,
            },
        ],
    )
    result = list_highlights_clips("07-07-2026_10h00")
    assert result["ok"] is True
    assert result["session_folder"] == "07-07-2026_10h00"
    assert result["clip_count"] == 1
    assert result["clips"][0]["hype_score"] == 0.9


def test_list_highlights_clips_missing_returns_error(tmp_path, monkeypatch) -> None:
    from ai_video_editor.config import settings

    monkeypatch.setattr(settings, "workspace_dir", tmp_path)
    (tmp_path / "highlights" / "Valorant").mkdir(parents=True)
    result = list_highlights_clips("does_not_exist")
    assert result["ok"] is False
    assert "not found" in result["error"]


# ---------------------------------------------------------------------
# Custom montage — validation only (render mocked)
# ---------------------------------------------------------------------


def test_build_custom_montage_rejects_empty_list(tmp_path, monkeypatch) -> None:
    from ai_video_editor.config import settings
    from ai_video_editor.shorts import build_custom_montage

    monkeypatch.setattr(settings, "workspace_dir", tmp_path)
    result = build_custom_montage(clip_paths=[])
    assert result["ok"] is False
    assert "empty" in result["error"]


def test_build_custom_montage_skips_missing_paths(tmp_path, monkeypatch) -> None:
    from ai_video_editor.config import settings
    from ai_video_editor.shorts import build_custom_montage

    monkeypatch.setattr(settings, "workspace_dir", tmp_path)
    result = build_custom_montage(
        clip_paths=[
            "/does/not/exist.mp4",
            "/also/missing.mp4",
        ]
    )
    assert result["ok"] is False
    assert "none of the" in result["error"]
    assert set(result["skipped"]) == {
        "/does/not/exist.mp4",
        "/also/missing.mp4",
    }


def test_build_custom_montage_calls_render_with_resolved_paths(tmp_path, monkeypatch) -> None:
    """One good path, one bad — only the good one reaches render."""
    from ai_video_editor.config import settings
    from ai_video_editor.shorts import build_custom_montage

    monkeypatch.setattr(settings, "workspace_dir", tmp_path)
    good = tmp_path / "clip.mp4"
    _make_mp4(good, size=1024)

    monkeypatch.setattr(
        "ai_video_editor.candidates.probe.get_duration_seconds",
        lambda p: 20.0,
    )

    render_calls: list[int] = []

    def _fake_render(plan, out_path, **kwargs):
        render_calls.append(len(plan.clips))
        from ai_video_editor.shorts import RenderResult

        return RenderResult(plan=plan, out_path=out_path, ok=True)

    monkeypatch.setattr("ai_video_editor.shorts._render_and_refine", _fake_render)
    result = build_custom_montage(clip_paths=[str(good), "/missing.mp4"], name="unit_test")
    assert result["ok"] is True
    assert result["clips_resolved"] == 1
    assert result["clips_skipped"] == 1
    assert render_calls == [1]  # render called with 1 clip


# ---------------------------------------------------------------------
# List custom shorts on disk
# ---------------------------------------------------------------------


def test_list_custom_shorts_empty(tmp_path, monkeypatch) -> None:
    from ai_video_editor.config import settings

    monkeypatch.setattr(settings, "workspace_dir", tmp_path)
    assert list_custom_shorts() == {"folders": []}


def test_list_custom_shorts_returns_folders(tmp_path, monkeypatch) -> None:
    from ai_video_editor.config import settings

    monkeypatch.setattr(settings, "workspace_dir", tmp_path)
    d = tmp_path / "shorts" / "_custom" / "my_edit"
    d.mkdir(parents=True)
    _make_mp4(d / "my_edit.mp4")
    (d / "index.md").write_text("hi", encoding="utf-8")
    result = list_custom_shorts()
    assert len(result["folders"]) == 1
    assert result["folders"][0]["name"] == "my_edit"
    assert "my_edit.mp4" in result["folders"][0]["shorts"]


# ---------------------------------------------------------------------
# burn_title parameter tests
# ---------------------------------------------------------------------


def test_build_custom_montage_passes_burn_title_to_render(tmp_path, monkeypatch) -> None:
    """When burn_title is explicitly passed, it reaches the render function."""
    from ai_video_editor.config import settings
    from ai_video_editor.shorts import build_custom_montage

    monkeypatch.setattr(settings, "workspace_dir", tmp_path)
    clip = tmp_path / "clip.mp4"
    _make_mp4(clip, size=1024)

    monkeypatch.setattr(
        "ai_video_editor.candidates.probe.get_duration_seconds",
        lambda p: 10.0,
    )

    captured_kwargs: list[dict] = []

    def _fake_render(plan, out_path, **kwargs):
        captured_kwargs.append(kwargs)
        from ai_video_editor.shorts import RenderResult

        return RenderResult(plan=plan, out_path=out_path, ok=True)

    monkeypatch.setattr("ai_video_editor.shorts._render_and_refine", _fake_render)
    result = build_custom_montage(clip_paths=[str(clip)], name="burn_test", burn_title=False)
    assert result["ok"] is True
    assert len(captured_kwargs) == 1
    assert captured_kwargs[0].get("burn_title") is False


def test_build_custom_montage_burn_title_default_true(tmp_path, monkeypatch) -> None:
    """When burn_title is not passed, it defaults to True."""
    from ai_video_editor.config import settings
    from ai_video_editor.shorts import build_custom_montage

    monkeypatch.setattr(settings, "workspace_dir", tmp_path)
    clip = tmp_path / "clip.mp4"
    _make_mp4(clip, size=1024)

    monkeypatch.setattr(
        "ai_video_editor.candidates.probe.get_duration_seconds",
        lambda p: 10.0,
    )

    captured_kwargs: list[dict] = []

    def _fake_render(plan, out_path, **kwargs):
        captured_kwargs.append(kwargs)
        from ai_video_editor.shorts import RenderResult

        return RenderResult(plan=plan, out_path=out_path, ok=True)

    monkeypatch.setattr("ai_video_editor.shorts._render_and_refine", _fake_render)
    result = build_custom_montage(clip_paths=[str(clip)], name="default_test")
    assert result["ok"] is True
    assert len(captured_kwargs) == 1
    assert captured_kwargs[0].get("burn_title") is True
