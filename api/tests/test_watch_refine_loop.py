"""Tests for the watch_montage refine loop.

The concrete `render_short` + `watch_montage` calls are injected via
callbacks / a fake watch_fn — this suite verifies the loop's control
flow and flag-to-drop translation without ffmpeg or a live VLM.
"""

from __future__ import annotations

from pathlib import Path

from ai_video_editor.vlm.loops import (
    _clip_indices_to_drop,
    _clip_timeline_from_durations,
    watch_and_refine_montage,
)
from ai_video_editor.vlm.watch_montage import (
    MomentFlag,
    MontageCritique,
    WatchResult,
)

# ---------------------------------------------------------------------
# Timeline math
# ---------------------------------------------------------------------


def test_timeline_cumulative_ranges() -> None:
    assert _clip_timeline_from_durations([4.0, 6.0, 5.0]) == [
        (0.0, 4.0),
        (4.0, 10.0),
        (10.0, 15.0),
    ]


def test_timeline_zero_duration_clip_produces_zero_range() -> None:
    assert _clip_timeline_from_durations([3.0, 0.0, 2.0]) == [
        (0.0, 3.0),
        (3.0, 3.0),
        (3.0, 5.0),
    ]


def test_timeline_empty_list_returns_empty() -> None:
    assert _clip_timeline_from_durations([]) == []


# ---------------------------------------------------------------------
# Flag -> clip drop mapping
# ---------------------------------------------------------------------


def _flag(cat: str, start: float, end: float) -> MomentFlag:
    return MomentFlag(start_s=start, end_s=end, category=cat, why="test")


def test_negative_flag_in_clip_marks_it_for_drop() -> None:
    timeline = [(0.0, 4.0), (4.0, 8.0)]
    flags = [_flag("scoreboard", 4.0, 8.0)]
    assert _clip_indices_to_drop(flags, timeline) == [1]


def test_positive_flag_protects_clip_from_drop() -> None:
    """A great_moment in the same clip as a scoreboard flag should
    protect the clip -- scoreboards are OK when the payoff is there."""
    timeline = [(0.0, 4.0), (4.0, 8.0)]
    flags = [
        _flag("scoreboard", 4.0, 6.0),
        _flag("great_moment", 6.0, 8.0),
    ]
    assert _clip_indices_to_drop(flags, timeline) == []


def test_flag_spanning_boundary_drops_first_clip_it_touches() -> None:
    """A flag starting at 3s (clip 0) and ending at 6s (clip 1) touches
    both -- both flagged as negative."""
    timeline = [(0.0, 4.0), (4.0, 8.0), (8.0, 12.0)]
    flags = [_flag("dead_air", 3.0, 6.0)]
    assert _clip_indices_to_drop(flags, timeline) == [0, 1]


def test_no_negative_flags_returns_empty() -> None:
    timeline = [(0.0, 4.0), (4.0, 8.0)]
    flags = [_flag("great_moment", 0.0, 4.0)]
    assert _clip_indices_to_drop(flags, timeline) == []


def test_all_five_negative_categories_drop() -> None:
    """Each negative category should trigger a drop when alone in
    its clip's window."""
    for cat in (
        "dead_air", "scoreboard", "own_death",
        "teammate_kill_only", "boring_pacing",
    ):
        timeline = [(0.0, 4.0)]
        assert _clip_indices_to_drop([_flag(cat, 0.0, 4.0)], timeline) == [0]


# ---------------------------------------------------------------------
# Loop control flow (mocked watch_fn)
# ---------------------------------------------------------------------


def _watch_result(
    verdict: str,
    flags: list[MomentFlag] | None = None,
) -> WatchResult:
    critique = MontageCritique(
        overall_verdict=verdict,
        opener_strength=3,
        climax_strength=3,
        pacing="just_right",
        top_recommendation="test",
        flags=flags or [],
    )
    return WatchResult(critique=critique, panel_count=1, total_seconds=10.0)


def _run_loop(
    watch_results: list[WatchResult],
    clip_durations: list[float] | None = None,
    clip_names: list[str] | None = None,
    max_passes: int = 5,
    render_ok: bool = True,
    render_err: str | None = None,
) -> tuple:
    """Drive the loop with a canned list of watch verdicts. Returns
    (result, render_calls, watch_calls). render_calls[0] is None for
    the initial render, then a kept-indices list for each re-render."""
    watch_results = list(watch_results)
    render_calls: list[list[int] | None] = []
    watch_calls: list[Path] = []

    def _fake_watch(path: str | Path) -> WatchResult:
        watch_calls.append(Path(path))
        return watch_results.pop(0) if watch_results else _watch_result("ship_it")

    def _initial() -> tuple[bool, str | None, Path]:
        render_calls.append(None)
        return render_ok, render_err, Path("/tmp/out.mp4")

    def _re_render(kept: list[int]) -> tuple[bool, str | None, Path]:
        render_calls.append(kept)
        return render_ok, render_err, Path("/tmp/out.mp4")

    result = watch_and_refine_montage(
        initial_render=_initial,
        re_render_after_drop=_re_render,
        clip_durations=clip_durations or [4.0, 4.0, 4.0, 4.0],
        clip_names=clip_names or ["a.mp4", "b.mp4", "c.mp4", "d.mp4"],
        max_passes=max_passes,
        watch_fn=_fake_watch,
    )
    return result, render_calls, watch_calls


def test_ship_it_on_first_pass_returns_immediately() -> None:
    result, renders, watches = _run_loop([_watch_result("ship_it")])
    assert result.ok is True
    assert result.passes == 1
    assert result.stopped_reason == "ship_it"
    assert result.dropped_clips == []
    assert len(renders) == 1  # initial only
    assert len(watches) == 1


def test_start_over_bails_immediately() -> None:
    result, renders, _ = _run_loop([_watch_result("start_over")])
    assert result.stopped_reason == "start_over"
    assert result.dropped_clips == []
    assert len(renders) == 1


def test_needs_edits_drops_flagged_clip_and_re_renders() -> None:
    # Panel spans t=4-8s which is clip index 1 ("b.mp4"). Second pass
    # ships.
    scoreboard_flag = MomentFlag(
        start_s=4.0, end_s=8.0, category="scoreboard", why="post-round"
    )
    result, renders, watches = _run_loop(
        [
            _watch_result("needs_edits", [scoreboard_flag]),
            _watch_result("ship_it"),
        ]
    )
    assert result.stopped_reason == "ship_it"
    assert result.passes == 2
    assert result.dropped_clips == ["b.mp4"]
    # Initial + re-render.
    assert renders[0] is None
    assert renders[1] == [0, 2, 3]  # kept indices after dropping clip 1
    assert len(watches) == 2


def test_needs_edits_with_no_actionable_flags_returns_no_drops_possible() -> None:
    """needs_edits but only positive flags -> no drops, ship best-effort."""
    result, renders, _ = _run_loop(
        [
            _watch_result(
                "needs_edits",
                [MomentFlag(start_s=0.0, end_s=4.0, category="good_buildup", why="")],
            )
        ]
    )
    assert result.stopped_reason == "no_drops_possible"
    assert result.dropped_clips == []
    assert len(renders) == 1


def test_would_empty_plan_guards_against_dropping_to_below_2_clips() -> None:
    """If flagging would drop us below 2 clips, stop and ship what
    we have -- better than a single-clip montage."""
    result, renders, _ = _run_loop(
        [
            _watch_result(
                "needs_edits",
                [
                    _flag("scoreboard", 0.0, 4.0),
                    _flag("dead_air", 4.0, 8.0),
                    _flag("own_death", 8.0, 12.0),
                ],
            )
        ],
        clip_durations=[4.0, 4.0, 4.0, 4.0],  # 3 dropped -> 1 remaining
    )
    assert result.stopped_reason == "would_empty_plan"
    assert result.dropped_clips == []
    assert len(renders) == 1


def test_cap_reached_when_loop_never_reaches_ship_it() -> None:
    """5 passes, each dropping one clip; if the montage never gets
    a ship_it verdict, the cap fires."""
    def _drop_flag(clip_time_start: float) -> MomentFlag:
        return _flag("scoreboard", clip_time_start, clip_time_start + 4.0)

    # Enough clips + drops to survive 3 iterations without emptying.
    result, renders, _ = _run_loop(
        [
            _watch_result("needs_edits", [_drop_flag(4.0)]),   # drops b -> a,c,d
            _watch_result("needs_edits", [_drop_flag(4.0)]),   # drops c -> a,d
            _watch_result("needs_edits", [_drop_flag(4.0)]),   # would empty -> a alone
        ],
        clip_durations=[4.0, 4.0, 4.0, 4.0],
        max_passes=5,
    )
    # After 2 successful drops we get a would_empty on pass 3.
    assert result.dropped_clips == ["b.mp4", "c.mp4"]
    assert result.stopped_reason == "would_empty_plan"
    assert result.passes == 3
    assert renders == [None, [0, 2, 3], [0, 3]]


def test_vlm_unavailable_returns_gracefully() -> None:
    result, renders, _ = _run_loop(
        [WatchResult(critique=None, panel_count=0, total_seconds=0.0, error="oops")]
    )
    assert result.ok is True
    assert result.final_verdict == "vlm_unavailable"
    assert result.stopped_reason == "vlm_unavailable"
    assert result.render_error == "oops"
    assert len(renders) == 1


def test_render_failure_short_circuits_the_loop() -> None:
    result, renders, watches = _run_loop(
        [_watch_result("ship_it")],
        render_ok=False, render_err="ffmpeg exited 1",
    )
    assert result.ok is False
    assert result.stopped_reason == "render_failed"
    assert result.render_error == "ffmpeg exited 1"
    assert len(renders) == 1
    assert watches == []  # never got to watch
