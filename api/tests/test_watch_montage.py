"""Tests for the whole-montage watch critic.

Pure tiling math + save-to-markdown tests. The VLM call is mocked
(covered end-to-end by manual smoke tests on real montages).
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from ai_video_editor.vlm.watch_montage import (
    MomentFlag,
    MontageCritique,
    Panel,
    WatchResult,
    _snap_to_panel,
    render_critique_markdown,
    save_critique,
)


def _panel(idx: int, start_s: float, panel_seconds: float = 4.0) -> Panel:
    return Panel(
        path=Path(f"/tmp/panel_{idx:03d}.jpg"),
        start_s=start_s,
        end_s=start_s + panel_seconds,
        index=idx,
    )


def test_snap_hits_exact_boundary() -> None:
    panels = [_panel(1, 0), _panel(2, 4), _panel(3, 8)]
    assert _snap_to_panel(4.0, panels) == 4.0
    assert _snap_to_panel(0.0, panels) == 0.0


def test_snap_rounds_to_nearest_boundary() -> None:
    panels = [_panel(1, 0), _panel(2, 4), _panel(3, 8)]
    assert _snap_to_panel(3.3, panels) == 4.0
    assert _snap_to_panel(5.9, panels) == 4.0
    assert _snap_to_panel(6.1, panels) == 8.0


def test_snap_empty_panel_list_returns_input() -> None:
    assert _snap_to_panel(3.14, []) == 3.14


def test_render_markdown_success_shape() -> None:
    critique = MontageCritique(
        overall_verdict="needs_edits",
        opener_strength=3,
        climax_strength=4,
        pacing="just_right",
        top_recommendation="Trim the scoreboard at t=4-8s.",
        flags=[
            MomentFlag(start_s=4, end_s=8, category="scoreboard", why="post-round kda"),
            MomentFlag(start_s=48, end_s=52, category="great_moment", why="ace"),
        ],
    )
    result = WatchResult(critique=critique, panel_count=15, total_seconds=60.0)
    md = render_critique_markdown(result)
    assert "needs_edits" in md
    assert "3/5" in md and "4/5" in md
    assert "scoreboard" in md
    assert "great_moment" in md
    assert "Panels:** 15" in md


def test_render_markdown_error_shape() -> None:
    result = WatchResult(critique=None, panel_count=0, total_seconds=0, error="ffmpeg missing")
    md = render_critique_markdown(result)
    assert "ffmpeg missing" in md


def test_save_critique_writes_file(tmp_path) -> None:
    critique = MontageCritique(
        overall_verdict="ship_it",
        opener_strength=5,
        climax_strength=5,
        pacing="just_right",
        top_recommendation="Nothing to change.",
    )
    result = WatchResult(critique=critique, panel_count=8, total_seconds=32.0)
    target = tmp_path / "critique.md"
    save_critique(result, target)
    assert target.is_file()
    assert "ship_it" in target.read_text(encoding="utf-8")


def test_pipe_char_in_why_is_escaped(tmp_path) -> None:
    critique = MontageCritique(
        overall_verdict="needs_edits",
        opener_strength=2,
        climax_strength=3,
        pacing="too_fast",
        top_recommendation="rec",
        flags=[MomentFlag(start_s=0, end_s=4, category="scoreboard", why="a | b | c")],
    )
    result = WatchResult(critique=critique, panel_count=15, total_seconds=60.0)
    md = render_critique_markdown(result)
    assert "a \\| b \\| c" in md


def test_extract_panels_pillow_tiling(tmp_path) -> None:
    """End-to-end tiling math test using synthetic frames written by
    Pillow directly (skipping ffmpeg — the compose step is the part
    with real logic to verify)."""
    from ai_video_editor.vlm.watch_montage import _compose_mosaic

    frames = []
    for i in range(4):
        img = Image.new("RGB", (100, 50), color=(i * 50, 0, 0))
        p = tmp_path / f"f_{i:04d}.jpg"
        img.save(p, "JPEG", quality=85)
        frames.append(p)

    out = tmp_path / "mosaic.jpg"
    _compose_mosaic(frames, out, grid=(2, 2), quality=82)
    assert out.is_file()
    with Image.open(out) as m:
        assert m.size == (200, 100)  # 2x2 of 100x50 cells
