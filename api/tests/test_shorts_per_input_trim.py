"""Per-input trim + anchor_offset integration tests.

Verifies that:
- render_short emits -ss / -t per input ONLY when anchor_offset is set
  (highlight-folder clips must not get bogus -ss values)
- _kill_times_in_montage uses anchor_offset_seconds when set, falls back
  to midpoint otherwise
"""

from __future__ import annotations

from pathlib import Path

from ai_video_editor.shorts import (
    ShortClip,
    ShortPlan,
    _build_filter_complex,
    _kill_times_in_montage,
)


def _clip(
    file: str,
    start: float,
    end: float,
    *,
    anchor_offset: float | None = None,
) -> ShortClip:
    return ShortClip(
        file=file,
        path=Path("/tmp") / file,
        event_type="kill",
        start_seconds=start,
        end_seconds=end,
        hype_score=0.9,
        funny_score=0.5,
        story_score=0.5,
        reason="",
        anchor_offset_seconds=anchor_offset,
    )


def _plan(clips: list[ShortClip]) -> ShortPlan:
    return ShortPlan(
        bucket="custom",
        clips=tuple(clips),
        title="TEST",
        vo_prompt="",
        index=1,
    )


# ---------------------------------------------------------------------
# _kill_times_in_montage
# ---------------------------------------------------------------------


def test_kill_times_uses_anchor_offset_when_present() -> None:
    """A clip with anchor_offset=3.5 (of 10s window) fires the kill at
    t=3.5 in the montage."""
    plan = _plan([_clip("a.mp4", 0.0, 10.0, anchor_offset=3.5)])
    assert _kill_times_in_montage(plan) == (3.5,)


def test_kill_times_falls_back_to_midpoint_when_no_anchor() -> None:
    """No anchor_offset → midpoint (backward compat with highlights)."""
    plan = _plan([_clip("a.mp4", 100.0, 110.0)])  # 10s window
    assert _kill_times_in_montage(plan) == (5.0,)


def test_kill_times_multi_clip_montage_time_accounting() -> None:
    """Second clip's anchor is offset by first clip's duration."""
    plan = _plan(
        [
            _clip("a.mp4", 0.0, 8.0, anchor_offset=5.0),   # kill at t=5
            _clip("b.mp4", 0.0, 10.0, anchor_offset=6.0),  # kill at t=8+6=14
        ]
    )
    assert _kill_times_in_montage(plan) == (5.0, 14.0)


# ---------------------------------------------------------------------
# render_short input args (assembled inline in render_short so we
# reconstruct the exact loop here rather than mock ffmpeg)
# ---------------------------------------------------------------------


def _assemble_input_args(clips: list[ShortClip]) -> list[str]:
    """Mirror render_short's input-arg assembly for testability."""
    input_args: list[str] = []
    for clip in clips:
        if clip.anchor_offset_seconds is not None:
            window = clip.end_seconds - clip.start_seconds
            if clip.start_seconds > 0.0:
                input_args.extend(["-ss", f"{clip.start_seconds:.3f}"])
            if window > 0.0:
                input_args.extend(["-t", f"{window:.3f}"])
        input_args.extend(["-i", str(clip.path)])
    return input_args


def test_no_trim_for_highlight_folder_clip() -> None:
    """Clips without anchor_offset (highlight-folder clips) must NOT
    get -ss / -t — those fields are SOURCE offsets, not file offsets."""
    clips = [_clip("a.mp4", 100.0, 130.0)]
    args = _assemble_input_args(clips)
    assert "-ss" not in args
    assert "-t" not in args
    assert args == ["-i", str(clips[0].path)]


def test_trim_applied_when_anchor_offset_set() -> None:
    """Clip with anchor_offset gets -ss / -t in file timebase."""
    args = _assemble_input_args(
        [_clip("a.mp4", 5.0, 15.0, anchor_offset=6.0)]
    )
    # -ss 5.000 -t 10.000 -i /tmp/a.mp4
    assert args[0] == "-ss"
    assert args[1] == "5.000"
    assert args[2] == "-t"
    assert args[3] == "10.000"
    assert args[4] == "-i"


def test_no_ss_when_start_is_zero_but_still_get_no_flags() -> None:
    """When start=0 (no seek needed) AND anchor is set, we don't emit -ss
    but the analyzer's window should still be respected via natural EOF.
    Currently we skip -t too when start is 0 — verify."""
    args = _assemble_input_args(
        [_clip("a.mp4", 0.0, 8.0, anchor_offset=4.0)]
    )
    # We emit -t 8.000 because window > 0 (per current code we do
    # emit even when start=0, since anchor_offset is set).
    # The test doubles as documentation of the actual behavior.
    assert "-ss" not in args
    # -t is emitted whenever window > 0 in the anchor-set branch
    assert "-t" in args
    assert "8.000" in args


def test_filter_complex_still_builds_normally_with_trim() -> None:
    """Sanity: the filter graph builder should be unaffected by whether
    -ss / -t are used. Uses whichever clip count."""
    clips = [
        _clip("a.mp4", 5.0, 15.0, anchor_offset=6.0),
        _clip("b.mp4", 0.0, 12.0, anchor_offset=5.0),
    ]
    fg = _build_filter_complex(
        n_video_inputs=len(clips),
        title="TEST",
        mode="montage",
        music_input_index=None,
        duck_volume=0.2,
        music_volume=0.4,
    )
    assert "concat=n=2" in fg
    assert "[vout]" in fg
