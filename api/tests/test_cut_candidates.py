"""cut_candidates: pure option proposal + facts. No ffmpeg / no speech text."""

from __future__ import annotations

from ai_video_editor.cut_candidates import (
    WordSpan,
    compute_facts,
    duration_range,
    fallback_padding_window,
    propose_cut_options,
)
from ai_video_editor.cut_signals import CutSignals, LoudnessStep, QuietSpan


def _signals(
    *,
    quiet: list[tuple[float, float]] | None = None,
    loud: list[tuple[float, float]] | None = None,
    scenes: list[float] | None = None,
) -> CutSignals:
    return CutSignals(
        quiet_spans=[QuietSpan(a, b) for a, b in (quiet or [])],
        loudness=[LoudnessStep(t, level) for t, level in (loud or [])],
        scene_changes=list(scenes or []),
    )


def test_propose_includes_padding_and_three_to_five_options():
    sig = _signals(
        quiet=[(7.0, 8.0), (18.0, 19.0)],
        loud=[(10.5, -18.0), (11.0, -16.0)],
        scenes=[8.1, 17.9],
    )
    opts = propose_cut_options(12.0, "kill", sig, duration_seconds=60.0)
    assert 3 <= len(opts) <= 5
    assert any(o.label == "padding" for o in opts)
    for o in opts:
        assert o.start <= 12.0 <= o.end
        assert o.facts.duration == round(o.end - o.start, 3)


def test_assist_and_death_search_wider_than_kill():
    sig = _signals(quiet=[(1.0, 2.0), (28.0, 29.0)], scenes=[2.5, 27.0])
    kill_opts = propose_cut_options(15.0, "kill", sig, duration_seconds=60.0)
    assist_opts = propose_cut_options(15.0, "assist", sig, duration_seconds=60.0)
    death_opts = propose_cut_options(15.0, "death", sig, duration_seconds=60.0)
    kill_span = max(o.end - o.start for o in kill_opts)
    assist_span = max(o.end - o.start for o in assist_opts)
    death_span = max(o.end - o.start for o in death_opts)
    assert assist_span >= kill_span
    assert death_span >= kill_span
    assert any(o.label == "wide_padding" for o in assist_opts)


def test_facts_cover_required_fields_and_quiet_edges():
    sig = _signals(
        quiet=[(9.5, 10.2), (20.0, 20.5)],
        loud=[(10.5, -15.0), (11.0, -14.0)],
    )
    facts = compute_facts(10.0, 17.0, 12.0, sig)
    assert facts.seconds_before_event == 2.0
    assert facts.seconds_after_event == 5.0
    assert facts.duration == 7.0
    assert facts.starts_on_quiet is True
    assert facts.ends_on_quiet is False
    assert facts.loud_peak_within_first_2s is True
    assert facts.cuts_mid_speech is None


def test_cuts_mid_speech_uses_timestamps_only():
    sig = _signals()
    words = [WordSpan(10.2, 10.6), WordSpan(14.0, 14.4)]
    # Start cuts through first word.
    facts = compute_facts(10.4, 15.0, 12.0, sig, words)
    assert facts.cuts_mid_speech is True
    # Clean cuts outside words.
    clean = compute_facts(11.0, 13.5, 12.0, sig, words)
    assert clean.cuts_mid_speech is False
    # State dict is facts only — never speech text.
    state = clean.as_state_dict()
    assert "text" not in state
    assert set(state) <= {
        "seconds_before_event",
        "seconds_after_event",
        "duration",
        "longest_dead_air",
        "starts_on_quiet",
        "ends_on_quiet",
        "loud_peak_within_first_2s",
        "cuts_through_loud_span",
        "cuts_mid_speech",
    }


def test_fallback_padding_matches_event_overrides():
    # kill override default (3.0, 8.0)
    s, e = fallback_padding_window(100.0, "kill", 200.0)
    assert s == 97.0 and e == 108.0


def test_duration_range_is_positive_and_bounded():
    lo, hi = duration_range("kill")
    assert 0 < lo < hi
