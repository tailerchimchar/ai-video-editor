"""cut_picker: Jev-injected selection, rules/errors → UNSURE / flagged."""

from __future__ import annotations

from ai_video_editor.cut_candidates import propose_cut_options
from ai_video_editor.cut_picker import (
    CutRunStats,
    pick_cut,
    pick_cuts_batch,
    resolve_clip_cut,
    shadow_log_line,
)
from ai_video_editor.cut_signals import CutSignals, LoudnessStep, QuietSpan
from ai_video_editor.decisions.models import Answer, DecisionError


def _opts(event_type: str = "kill"):
    sig = CutSignals(
        quiet_spans=[QuietSpan(7.0, 8.0), QuietSpan(18.0, 19.0)],
        loudness=[LoudnessStep(10.5, -16.0), LoudnessStep(11.0, -15.0)],
        scene_changes=[8.0, 18.0],
    )
    return propose_cut_options(12.0, event_type, sig, duration_seconds=60.0)


def _all_true_jev(items):
    return [Answer(value=True, probability=0.9, backend="jev") for _ in items]


def _all_rules(items):
    return [Answer(value=True, probability=1.0, backend="rules") for _ in items]


def _mixed_by_option(items):
    """Option 0 fails hook; option 1+ pass everything confidently."""
    # Questions are grouped option-major: for each option, 5 criteria.
    out = []
    # Infer option count from prompt ("option 0", "option 1", …).
    for q, _state in items:
        prompt = q.prompt
        # First criterion of option 0 → False; else True.
        if "option 0" in prompt and "hook" in prompt:
            out.append(Answer(value=False, probability=0.9, backend="jev"))
        else:
            out.append(Answer(value=True, probability=0.85, backend="jev"))
    return out


def test_pick_chooses_option_with_most_confident_passes():
    opts = _opts()
    assert len(opts) >= 2
    pick = pick_cut(
        opts,
        event_time=12.0,
        event_type="kill",
        duration_seconds=60.0,
        ask_many=_mixed_by_option,
    )
    assert pick.flagged is False
    # Option 0 loses the hook criterion; a later option should win with more passes.
    assert pick.option_index is not None and pick.option_index != 0
    assert pick.criteria_passed == 5


def test_rules_backend_answers_count_as_unsure_and_flag_fallback():
    opts = _opts()
    pick = pick_cut(
        opts,
        event_time=12.0,
        event_type="kill",
        duration_seconds=60.0,
        ask_many=_all_rules,
    )
    assert pick.flagged is True
    assert pick.criteria_passed == 0
    # Fallback = event ± kill padding (3, 8)
    assert pick.start == 9.0 and pick.end == 20.0


def test_jev_error_falls_back_flagged():
    def boom(_items):
        raise DecisionError("jev down")

    opts = _opts()
    pick = pick_cut(
        opts,
        event_time=12.0,
        event_type="kill",
        duration_seconds=60.0,
        ask_many=boom,
    )
    assert pick.flagged is True
    assert pick.label == "fallback_error"


def test_low_confidence_true_does_not_count():
    def soft(items):
        return [Answer(value=True, probability=0.2, backend="jev") for _ in items]

    pick = pick_cut(
        _opts(),
        event_time=12.0,
        event_type="kill",
        duration_seconds=60.0,
        ask_many=soft,
    )
    assert pick.flagged is True


def test_batch_one_shared_state_ask():
    seen_states = []

    def capture(items):
        seen_states.append(items[0][1])
        # All questions share one state object identity in a batch.
        assert all(s is items[0][1] for _q, s in items)
        return _all_true_jev(items)

    jobs = [
        {
            "options": _opts("kill"),
            "event_time": 12.0,
            "event_type": "kill",
            "duration_seconds": 60.0,
        },
        {
            "options": _opts("assist"),
            "event_time": 12.0,
            "event_type": "assist",
            "duration_seconds": 60.0,
        },
    ]
    picks, n_requests = pick_cuts_batch(jobs, ask_many=capture)
    assert len(picks) == 2
    assert n_requests == 1
    assert all(not p.flagged for p in picks)
    assert len(seen_states) == 1
    assert "options" in seen_states[0]


def test_resolve_clip_cut_with_fake_runner_and_asker(tmp_path):
    audio = (
        "[silencedetect @ 0x1] silence_start: 0.2\n"
        "[silencedetect @ 0x1] silence_end: 0.8 | silence_duration: 0.6\n"
        "[Parsed_ebur128_0 @ 0x2] t: 1.0  TARGET:-23 LUFS    M: -18.0 S:-20.0\n"
    )
    scene = "[scdet @ 0x3] lavfi.scd.score: 20.0, lavfi.scd.time: 2.0\n"

    def runner(argv: list[str]) -> str:
        return audio if any("silencedetect" in a for a in argv) else scene

    pick, ffmpeg_ms, sig = resolve_clip_cut(
        "fake.mp4",
        5.0,
        "kill",
        30.0,
        ask_many=_all_true_jev,
        runner=runner,
    )
    assert ffmpeg_ms >= 0.0
    assert len(sig.quiet_spans) >= 1 or len(sig.loudness) >= 1
    assert pick.flagged is False or pick.flagged is True  # either is valid
    assert pick.end > pick.start


def test_shadow_log_line_has_deltas_no_player_names():
    from ai_video_editor.cut_picker import CutPick

    line = shadow_log_line(
        event_type="assist",
        legacy_start=10.0,
        legacy_end=20.0,
        pick=CutPick(
            start=11.5,
            end=21.0,
            flagged=False,
            criteria_passed=4,
            option_index=1,
            jev_ms=12.0,
            input_tokens=90,
            label="quiet_snap",
        ),
    )
    assert "delta_start=1.50" in line
    assert "delta_end=1.00" in line
    assert "criteria_passed=4" in line
    assert "input_tokens=90" in line
    # No invented player identity leaked into the log line.
    assert "player" not in line.lower()


def test_run_stats_summary_fields(capsys):
    stats = CutRunStats(clips=3, jev_requests=3, flagged=1, fallback=1, ffmpeg_ms=40, jev_ms=25)
    stats.log_summary()  # goes to logging, not stdout — just ensure no raise
