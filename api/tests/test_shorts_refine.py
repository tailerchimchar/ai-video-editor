"""Auto-fix loop tests — the shorts pipeline's whole-comp review →
re-cut → re-render mechanics.

No live VLM, no ffmpeg. Mock `render_short`, `_run_vlm_review`, and
`trim_clip` so we can assert on control flow.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from ai_video_editor.shorts import (
    ShortClip,
    ShortPlan,
    _apply_window_fix_to_clip,
    _apply_window_fixes,
    _render_and_refine,
    _resolve_clip_ref,
)


class _Fix:
    """Duck-typed CompilationFix — matches the fields the refine loop reads."""

    def __init__(
        self,
        clip_ref: str = "01",
        fix: str = "extend_before",
        fix_seconds: float = 2.0,
        issue: str = "no setup",
    ) -> None:
        self.clip_ref = clip_ref
        self.fix = fix
        self.fix_seconds = fix_seconds
        self.issue = issue


class _Review:
    """Duck-typed CompilationReview."""

    def __init__(self, *, is_cohesive: bool, fixes: list | None = None) -> None:
        self.is_cohesive = is_cohesive
        self.fixes = fixes or []


def _clip(file: str = "01_kill_10m00s.mp4", start: float = 600, end: float = 620) -> ShortClip:
    return ShortClip(
        file=file,
        path=Path("/tmp") / file,
        event_type="kill",
        start_seconds=start,
        end_seconds=end,
        hype_score=0.8,
        funny_score=0.5,
        story_score=0.5,
        reason="test",
    )


def _plan(clip: ShortClip | None = None) -> ShortPlan:
    return ShortPlan(
        bucket="mid_game",
        clips=(clip or _clip(),),
        title="MID GAME KILL",
        vo_prompt="What I was thinking",
        index=1,
    )


# ---------------------------------------------------------------------
# _resolve_clip_ref
# ---------------------------------------------------------------------


def test_resolve_clip_ref_numeric_1_based() -> None:
    assert _resolve_clip_ref("01", 3) == 0
    assert _resolve_clip_ref("02", 3) == 1
    assert _resolve_clip_ref("3", 3) == 2


def test_resolve_clip_ref_out_of_range_falls_back_to_zero() -> None:
    assert _resolve_clip_ref("99", 3) == 0


def test_resolve_clip_ref_time_or_uuid_falls_back_to_zero() -> None:
    assert _resolve_clip_ref("2:34", 3) == 0
    assert _resolve_clip_ref("abc12345", 3) == 0


# ---------------------------------------------------------------------
# _apply_window_fix_to_clip — pure window math + trim_clip driver
# ---------------------------------------------------------------------


def test_apply_window_fix_extend_before(tmp_path: Path) -> None:
    clip = _clip(start=100, end=110)
    fix = _Fix(fix="extend_before", fix_seconds=3.0)
    with patch("ai_video_editor.shorts.trim_clip", return_value=(True, None)) as tc:
        new = _apply_window_fix_to_clip(clip, fix, "src.mp4", tmp_path, iteration=1)
    assert new is not None
    assert new.start_seconds == 97.0
    assert new.end_seconds == 110.0
    tc.assert_called_once()


def test_apply_window_fix_extend_after(tmp_path: Path) -> None:
    clip = _clip(start=100, end=110)
    fix = _Fix(fix="extend_after", fix_seconds=4.0)
    with patch("ai_video_editor.shorts.trim_clip", return_value=(True, None)):
        new = _apply_window_fix_to_clip(clip, fix, "src.mp4", tmp_path, iteration=1)
    assert new is not None
    assert new.start_seconds == 100.0
    assert new.end_seconds == 114.0


def test_apply_window_fix_trim_end_bounded(tmp_path: Path) -> None:
    clip = _clip(start=100, end=101)  # only 1s wide
    fix = _Fix(fix="trim_end", fix_seconds=5.0)  # would push end below start
    with patch("ai_video_editor.shorts.trim_clip", return_value=(True, None)):
        new = _apply_window_fix_to_clip(clip, fix, "src.mp4", tmp_path, iteration=1)
    # Guard clamps end to start+0.5 minimum
    assert new is not None
    assert new.end_seconds == pytest.approx(100.5)


def test_apply_window_fix_extend_before_clamps_at_zero(tmp_path: Path) -> None:
    clip = _clip(start=1.0, end=10.0)
    fix = _Fix(fix="extend_before", fix_seconds=5.0)
    with patch("ai_video_editor.shorts.trim_clip", return_value=(True, None)):
        new = _apply_window_fix_to_clip(clip, fix, "src.mp4", tmp_path, iteration=1)
    assert new is not None
    assert new.start_seconds == 0.0


def test_apply_window_fix_unknown_type_returns_none(tmp_path: Path) -> None:
    clip = _clip()
    fix = _Fix(fix="apply_zoom")
    with patch("ai_video_editor.shorts.trim_clip", return_value=(True, None)):
        result = _apply_window_fix_to_clip(clip, fix, "src.mp4", tmp_path, iteration=1)
    assert result is None


def test_apply_window_fix_trim_failure_returns_none(tmp_path: Path) -> None:
    clip = _clip()
    fix = _Fix(fix="extend_before", fix_seconds=2.0)
    with patch("ai_video_editor.shorts.trim_clip", return_value=(False, "ffmpeg died")):
        result = _apply_window_fix_to_clip(clip, fix, "src.mp4", tmp_path, iteration=1)
    assert result is None


def test_apply_window_fix_writes_iter_suffix_filename(tmp_path: Path) -> None:
    """Refined files must land under a stable `_iterN` suffix so the
    audit trail can show which iteration produced which mp4."""
    clip = _clip(file="03_kill_20m00s.mp4")
    fix = _Fix(fix="extend_before", fix_seconds=2.0)
    captured: dict = {}

    def _mock_trim(src, out, s, e):
        captured["out"] = out
        return True, None

    with patch("ai_video_editor.shorts.trim_clip", side_effect=_mock_trim):
        new = _apply_window_fix_to_clip(clip, fix, "src.mp4", tmp_path, iteration=2)
    assert new is not None
    assert "_iter2" in captured["out"]
    assert new.file == "03_kill_20m00s_iter2.mp4"


# ---------------------------------------------------------------------
# _apply_window_fixes — applies to correct clip, counts applied
# ---------------------------------------------------------------------


def test_apply_window_fixes_skips_non_window_types(tmp_path: Path) -> None:
    clips = (_clip(),)
    fixes = [_Fix(fix="apply_zoom"), _Fix(fix="remove_clip")]
    with patch("ai_video_editor.shorts.trim_clip") as tc:
        new_clips, applied = _apply_window_fixes(
            clips, fixes, asset_path="src.mp4", out_dir=tmp_path, iteration=1
        )
    assert applied == 0
    assert new_clips == clips
    tc.assert_not_called()


def test_apply_window_fixes_counts_only_successes(tmp_path: Path) -> None:
    clips = (_clip(),)
    fixes = [
        _Fix(fix="extend_before", fix_seconds=2.0),
        _Fix(fix="extend_after", fix_seconds=1.0),
    ]

    # First trim_clip succeeds, second fails
    call_count = {"n": 0}

    def _mock_trim(src, out, s, e):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return True, None
        return False, "second failure"

    with patch("ai_video_editor.shorts.trim_clip", side_effect=_mock_trim):
        _new, applied = _apply_window_fixes(
            clips, fixes, asset_path="src.mp4", out_dir=tmp_path, iteration=1
        )
    assert applied == 1


# ---------------------------------------------------------------------
# _render_and_refine — full loop control flow
# ---------------------------------------------------------------------


def test_refine_loop_exits_immediately_on_cohesive(tmp_path: Path) -> None:
    """First render + first review says pass → no re-cut, no re-render."""
    plan = _plan()
    out = tmp_path / "short_01.mp4"
    asset = {"path": "src.mp4", "game": "league"}

    render_calls = {"n": 0}

    def _mock_render(*a, **kw):
        render_calls["n"] += 1
        out.write_bytes(b"x")
        return True, None

    with patch("ai_video_editor.shorts.render_short", side_effect=_mock_render), \
         patch("ai_video_editor.shorts._run_vlm_review", return_value=_Review(is_cohesive=True)):
        result = _render_and_refine(
            plan, out, mode="montage", asset=asset,
            music_path=None, run_vlm_check=True, max_iter=2,
        )

    assert result.ok is True
    assert result.vlm_verdict == "pass"
    assert render_calls["n"] == 1


def test_refine_loop_iterates_when_needs_review_with_window_fix(tmp_path: Path) -> None:
    plan = _plan()
    out = tmp_path / "short_01.mp4"
    asset = {"path": "src.mp4", "game": "league"}

    render_calls = {"n": 0}
    review_calls = {"n": 0}

    def _mock_render(*a, **kw):
        render_calls["n"] += 1
        out.write_bytes(b"x")
        return True, None

    def _mock_review(*a, **kw):
        review_calls["n"] += 1
        # First 2 calls say needs_review + window fix, third says cohesive
        if review_calls["n"] < 3:
            return _Review(
                is_cohesive=False,
                fixes=[_Fix(fix="extend_before", fix_seconds=2.0, clip_ref="01")],
            )
        return _Review(is_cohesive=True)

    with patch("ai_video_editor.shorts.render_short", side_effect=_mock_render), \
         patch("ai_video_editor.shorts._run_vlm_review", side_effect=_mock_review), \
         patch("ai_video_editor.shorts.trim_clip", return_value=(True, None)):
        result = _render_and_refine(
            plan, out, mode="montage", asset=asset,
            music_path=None, run_vlm_check=True, max_iter=2,
        )

    assert result.ok is True
    assert result.vlm_verdict == "pass"
    # Loop should render 3x: initial + 2 refinements
    assert render_calls["n"] == 3
    assert result.extras["refine_iterations"] == 2
    assert len(result.extras["refinements_applied"]) == 2


def test_refine_loop_hits_iteration_cap(tmp_path: Path) -> None:
    plan = _plan()
    out = tmp_path / "short_01.mp4"
    asset = {"path": "src.mp4", "game": "league"}

    def _always_needs_review(*a, **kw):
        return _Review(
            is_cohesive=False,
            fixes=[_Fix(fix="extend_before", fix_seconds=2.0)],
        )

    def _mock_render(*a, **kw):
        out.write_bytes(b"x")
        return True, None

    with patch("ai_video_editor.shorts.render_short", side_effect=_mock_render), \
         patch("ai_video_editor.shorts._run_vlm_review", side_effect=_always_needs_review), \
         patch("ai_video_editor.shorts.trim_clip", return_value=(True, None)):
        result = _render_and_refine(
            plan, out, mode="montage", asset=asset,
            music_path=None, run_vlm_check=True, max_iter=2,
        )

    assert result.ok is True
    # Never got to pass; cap reached
    assert result.vlm_verdict == "needs_review"
    assert result.extras["refine_stopped"] == "cap_reached"


def test_refine_loop_skips_when_only_non_window_fixes(tmp_path: Path) -> None:
    plan = _plan()
    out = tmp_path / "short_01.mp4"
    asset = {"path": "src.mp4", "game": "league"}

    def _mock_render(*a, **kw):
        out.write_bytes(b"x")
        return True, None

    with patch("ai_video_editor.shorts.render_short", side_effect=_mock_render), \
         patch(
             "ai_video_editor.shorts._run_vlm_review",
             return_value=_Review(
                 is_cohesive=False,
                 fixes=[_Fix(fix="apply_zoom"), _Fix(fix="remove_clip")],
             ),
         ):
        result = _render_and_refine(
            plan, out, mode="montage", asset=asset,
            music_path=None, run_vlm_check=True, max_iter=2,
        )

    assert result.ok is True
    assert result.vlm_verdict == "needs_review"
    assert result.extras["refine_stopped"] == "no_window_fixes"
    assert result.extras["unapplied_fix_types"] == ["apply_zoom", "remove_clip"]


def test_refine_loop_disabled_by_max_iter_zero(tmp_path: Path) -> None:
    """`max_iter=0` disables auto-fix — same behavior as the old
    fire-and-forget check: single render + single review, verdict logged
    but never acts."""
    plan = _plan()
    out = tmp_path / "short_01.mp4"
    asset = {"path": "src.mp4", "game": "league"}

    render_calls = {"n": 0}

    def _mock_render(*a, **kw):
        render_calls["n"] += 1
        out.write_bytes(b"x")
        return True, None

    with patch("ai_video_editor.shorts.render_short", side_effect=_mock_render), \
         patch(
             "ai_video_editor.shorts._run_vlm_review",
             return_value=_Review(
                 is_cohesive=False,
                 fixes=[_Fix(fix="extend_before", fix_seconds=2.0)],
             ),
         ):
        result = _render_and_refine(
            plan, out, mode="montage", asset=asset,
            music_path=None, run_vlm_check=True, max_iter=0,
        )

    assert render_calls["n"] == 1
    assert result.vlm_verdict == "needs_review"


def test_refine_loop_gracefully_degrades_when_vlm_unavailable(tmp_path: Path) -> None:
    plan = _plan()
    out = tmp_path / "short_01.mp4"
    asset = {"path": "src.mp4", "game": "league"}

    def _mock_render(*a, **kw):
        out.write_bytes(b"x")
        return True, None

    with patch("ai_video_editor.shorts.render_short", side_effect=_mock_render), \
         patch("ai_video_editor.shorts._run_vlm_review", return_value=None):
        result = _render_and_refine(
            plan, out, mode="montage", asset=asset,
            music_path=None, run_vlm_check=True, max_iter=2,
        )

    assert result.ok is True
    assert result.extras["refine_stopped"] == "vlm_unavailable"


def test_refine_loop_render_failure_propagates(tmp_path: Path) -> None:
    plan = _plan()
    out = tmp_path / "short_01.mp4"
    asset = {"path": "src.mp4", "game": "league"}

    with patch(
        "ai_video_editor.shorts.render_short",
        return_value=(False, "ffmpeg fell over"),
    ):
        result = _render_and_refine(
            plan, out, mode="montage", asset=asset,
            music_path=None, run_vlm_check=True, max_iter=2,
        )

    assert result.ok is False
    assert result.error == "ffmpeg fell over"
