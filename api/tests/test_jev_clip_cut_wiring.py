"""Flags default off; real anchors only; one Jev batch for N clips."""

from __future__ import annotations

import json

from ai_video_editor.config import settings
from ai_video_editor.cut_candidates import propose_cut_options
from ai_video_editor.cut_picker import CutRunStats, pick_cuts_batch
from ai_video_editor.cut_signals import CutSignals, LoudnessStep, QuietSpan
from ai_video_editor.decisions.models import Answer, DecisionError
from ai_video_editor.highlights import (
    _batch_jev_clip_cuts,
    _jev_seed_window,
    cut_window,
    event_time_for_cut,
)


def test_flags_default_off():
    assert settings.grokbot_jev_clip_cut_enabled is False
    assert settings.grokbot_jev_clip_cut_shadow is False


def test_event_time_from_overwolf_anchor_seconds():
    cand = {
        "source": "overwolf_game_event",
        "event_type": "assist",
        "metadata": {"anchor_seconds": 42.5, "overwolf_type": "assist"},
    }
    assert event_time_for_cut(cand) == 42.5


def test_event_time_from_riot_and_cv_kda_anchor_seconds():
    riot = {
        "source": "riot_api",
        "event_type": "kill",
        "metadata": {"anchor_seconds": 110.0, "riot_event_ms": 105000},
    }
    cv = {
        "source": "cv_kda",
        "event_type": "death",
        "metadata": {"anchor_seconds": 88.0},
    }
    assert event_time_for_cut(riot) == 110.0
    assert event_time_for_cut(cv) == 88.0


def test_event_time_none_without_anchor_never_midpoint():
    audio = {
        "source": "audio_peak",
        "event_type": "audio_peak",
        "start_seconds": 10.0,
        "end_seconds": 20.0,
        "metadata": {},
    }
    transcript = {
        "source": "transcript_keyword",
        "event_type": "hype_callout",
        "start_seconds": 5.0,
        "end_seconds": 15.0,
        "metadata": {"rationale": "cue"},
    }
    outplayed = {"source": "outplayed_clip", "metadata": {}}
    assert event_time_for_cut(audio) is None
    assert event_time_for_cut(transcript) is None
    assert event_time_for_cut(outplayed) is None


def test_no_anchor_skipped_keeps_legacy(monkeypatch):
    monkeypatch.setattr(settings, "grokbot_jev_clip_cut_enabled", True)
    monkeypatch.setattr(settings, "grokbot_jev_clip_cut_shadow", False)

    ask_calls: list = []

    def ask_many(items):
        ask_calls.append(items)
        return [Answer(value=True, probability=0.9, backend="jev") for _ in items]

    plan = [
        {
            "event": "audio_peak",
            "legacy_start": 10.0,
            "legacy_end": 20.0,
            "event_time": None,
            "start": 10.0,
            "end": 20.0,
        }
    ]
    stats = CutRunStats()
    _batch_jev_clip_cuts(
        path="x.mp4",
        duration=60.0,
        plan=plan,
        stats=stats,
        ask_many=ask_many,
        runner=lambda _argv: "",
    )
    assert plan[0]["start"] == 10.0 and plan[0]["end"] == 20.0
    assert stats.skipped_no_anchor == 1
    assert stats.jev_requests == 0
    assert ask_calls == []


def test_overwolf_anchor_used_not_legacy_midpoint(monkeypatch):
    """Event time is metadata.anchor_seconds, not midpoint of legacy window."""
    monkeypatch.setattr(settings, "grokbot_jev_clip_cut_enabled", True)
    monkeypatch.setattr(settings, "grokbot_jev_clip_cut_shadow", False)

    seen_times: list[float] = []
    seen_seeds: list[tuple[float | None, float | None]] = []

    def fake_prepare(path, event_time, event_type, duration_seconds, **kw):
        seen_times.append(event_time)
        seen_seeds.append((kw.get("seed_start"), kw.get("seed_end")))
        sig = CutSignals(
            quiet_spans=[QuietSpan(40.0, 41.0)],
            loudness=[LoudnessStep(43.0, -16.0)],
            scene_changes=[],
        )
        opts = propose_cut_options(
            event_time,
            event_type,
            sig,
            duration_seconds=duration_seconds,
            seed_start=kw.get("seed_start"),
            seed_end=kw.get("seed_end"),
        )
        job = {
            "options": opts,
            "event_time": event_time,
            "event_type": event_type,
            "duration_seconds": duration_seconds,
        }
        return job, 1.0, sig

    def ask_many(items):
        return [Answer(value=True, probability=0.9, backend="jev") for _ in items]

    monkeypatch.setattr(
        "ai_video_editor.highlights.prepare_clip_cut_job",
        fake_prepare,
    )
    plan = [
        {
            "event": "assist",
            "cand": {"event_type": "assist", "metadata": {"anchor_seconds": 42.5}},
            "legacy_start": 30.0,  # midpoint would be 37.5 — must NOT be used
            "legacy_end": 45.0,
            "event_time": 42.5,
            "start": 30.0,
            "end": 45.0,
        }
    ]
    stats = CutRunStats()
    _batch_jev_clip_cuts(
        path="x.mp4",
        duration=120.0,
        plan=plan,
        stats=stats,
        ask_many=ask_many,
    )
    assert seen_times == [42.5]
    assert seen_seeds == [(30.0, 45.0)]
    assert stats.skipped_no_anchor == 0
    assert stats.jev_requests == 1
    # Seeded options always cover the legacy union.
    assert plan[0]["start"] <= 30.0
    assert plan[0]["end"] >= 45.0


def test_batch_one_jev_request_for_n_clips(monkeypatch):
    monkeypatch.setattr(settings, "grokbot_jev_clip_cut_enabled", True)
    monkeypatch.setattr(settings, "grokbot_jev_clip_cut_shadow", False)

    ask_calls: list = []

    def ask_many(items):
        ask_calls.append(items)
        return [Answer(value=True, probability=0.9, backend="jev") for _ in items]

    def fake_prepare(path, event_time, event_type, duration_seconds, **kw):
        sig = CutSignals()
        opts = propose_cut_options(event_time, event_type, sig, duration_seconds=duration_seconds)
        return (
            {
                "options": opts,
                "event_time": event_time,
                "event_type": event_type,
                "duration_seconds": duration_seconds,
            },
            2.0,
            sig,
        )

    monkeypatch.setattr(
        "ai_video_editor.highlights.prepare_clip_cut_job",
        fake_prepare,
    )

    n = 4
    plan = [
        {
            "event": "kill",
            "legacy_start": 10.0 * i,
            "legacy_end": 10.0 * i + 11.0,
            "event_time": 10.0 * i + 5.0,
            "start": 10.0 * i,
            "end": 10.0 * i + 11.0,
        }
        for i in range(1, n + 1)
    ]
    stats = CutRunStats()
    _batch_jev_clip_cuts(
        path="x.mp4",
        duration=200.0,
        plan=plan,
        stats=stats,
        ask_many=ask_many,
    )
    assert len(ask_calls) == 1
    assert stats.jev_requests == 1
    assert stats.clips == n
    # Shared state across all questions in the single ask.
    assert all(s is ask_calls[0][0][1] for _q, s in ask_calls[0])


def test_batch_error_keeps_all_legacy(monkeypatch):
    monkeypatch.setattr(settings, "grokbot_jev_clip_cut_enabled", True)
    monkeypatch.setattr(settings, "grokbot_jev_clip_cut_shadow", False)

    def boom(_items):
        raise RuntimeError("jev down")

    def fake_prepare(path, event_time, event_type, duration_seconds, **kw):
        opts = propose_cut_options(
            event_time, event_type, CutSignals(), duration_seconds=duration_seconds
        )
        return (
            {
                "options": opts,
                "event_time": event_time,
                "event_type": event_type,
                "duration_seconds": duration_seconds,
            },
            1.0,
            CutSignals(),
        )

    monkeypatch.setattr(
        "ai_video_editor.highlights.prepare_clip_cut_job",
        fake_prepare,
    )
    plan = [
        {
            "event": "kill",
            "legacy_start": 7.0,
            "legacy_end": 18.0,
            "event_time": 10.0,
            "start": 7.0,
            "end": 18.0,
        },
        {
            "event": "assist",
            "legacy_start": 20.0,
            "legacy_end": 30.0,
            "event_time": 25.0,
            "start": 20.0,
            "end": 30.0,
        },
    ]
    stats = CutRunStats()
    _batch_jev_clip_cuts(
        path="x.mp4",
        duration=60.0,
        plan=plan,
        stats=stats,
        ask_many=boom,
    )
    assert plan[0]["start"] == 7.0 and plan[0]["end"] == 18.0
    assert plan[1]["start"] == 20.0 and plan[1]["end"] == 30.0
    assert stats.jev_requests == 0
    assert stats.fallback == 2


def test_shadow_keeps_legacy_and_logs(monkeypatch, caplog):
    import logging

    monkeypatch.setattr(settings, "grokbot_jev_clip_cut_enabled", False)
    monkeypatch.setattr(settings, "grokbot_jev_clip_cut_shadow", True)

    def fake_prepare(path, event_time, event_type, duration_seconds, **kw):
        opts = propose_cut_options(
            event_time, event_type, CutSignals(), duration_seconds=duration_seconds
        )
        return (
            {
                "options": opts,
                "event_time": event_time,
                "event_type": event_type,
                "duration_seconds": duration_seconds,
            },
            5.0,
            CutSignals(),
        )

    def ask_many(items):
        return [Answer(value=True, probability=0.9, backend="jev") for _ in items]

    monkeypatch.setattr(
        "ai_video_editor.highlights.prepare_clip_cut_job",
        fake_prepare,
    )
    plan = [
        {
            "event": "kill",
            "legacy_start": 7.0,
            "legacy_end": 18.0,
            "event_time": 10.0,
            "start": 7.0,
            "end": 18.0,
        }
    ]
    stats = CutRunStats()
    with caplog.at_level(logging.INFO):
        _batch_jev_clip_cuts(
            path="x.mp4",
            duration=60.0,
            plan=plan,
            stats=stats,
            ask_many=ask_many,
        )
    assert (plan[0]["start"], plan[0]["end"]) == (7.0, 18.0)
    assert any("jev_clip_cut shadow" in r.message for r in caplog.records)


def test_cut_window_unchanged_for_anchor(monkeypatch):
    monkeypatch.setattr(settings, "grokbot_jev_clip_cut_enabled", False)
    cand = {
        "source": "overwolf_game_event",
        "event_type": "kill",
        "metadata": {"anchor_seconds": 50.0},
    }
    s, e = cut_window(cand, {}, 200.0)
    assert s == 47.0 and e == 58.0


def test_flagged_pick_keeps_legacy_when_enabled(monkeypatch):
    monkeypatch.setattr(settings, "grokbot_jev_clip_cut_enabled", True)
    monkeypatch.setattr(settings, "grokbot_jev_clip_cut_shadow", False)

    def fake_prepare(path, event_time, event_type, duration_seconds, **kw):
        opts = propose_cut_options(
            event_time,
            event_type,
            CutSignals(),
            duration_seconds=duration_seconds,
            seed_start=kw.get("seed_start"),
            seed_end=kw.get("seed_end"),
        )
        return (
            {
                "options": opts,
                "event_time": event_time,
                "event_type": event_type,
                "duration_seconds": duration_seconds,
            },
            1.0,
            CutSignals(),
        )

    def all_rules(items):
        return [Answer(value=True, probability=1.0, backend="rules") for _ in items]

    monkeypatch.setattr(
        "ai_video_editor.highlights.prepare_clip_cut_job",
        fake_prepare,
    )
    plan = [
        {
            "event": "kill",
            "cand": {"event_type": "kill", "metadata": {"anchor_seconds": 10.0}},
            "legacy_start": 7.0,
            "legacy_end": 18.0,
            "event_time": 10.0,
            "start": 7.0,
            "end": 18.0,
        }
    ]
    stats = CutRunStats()
    _batch_jev_clip_cuts(
        path="x.mp4",
        duration=60.0,
        plan=plan,
        stats=stats,
        ask_many=all_rules,
    )
    assert (plan[0]["start"], plan[0]["end"]) == (7.0, 18.0)
    assert stats.flagged == 1
    assert stats.fallback == 1


def test_single_job_batch_raises_on_jev_error():
    """Batch path (incl. one clip) must raise so highlights keep legacy."""

    def boom(_items):
        raise DecisionError("jev down")

    jobs = [
        {
            "options": propose_cut_options(10.0, "kill", CutSignals(), duration_seconds=60.0),
            "event_time": 10.0,
            "event_type": "kill",
            "duration_seconds": 60.0,
        }
    ]
    try:
        pick_cuts_batch(jobs, ask_many=boom)
        raised = False
    except DecisionError:
        raised = True
    assert raised


def test_cluster_seed_covers_first_to_last_kill():
    cand = {
        "event_type": "ace",
        "metadata": {
            "anchor_seconds": 100.0,
            "first_kill_seconds": 90.0,
            "last_kill_seconds": 110.0,
        },
    }
    # Midpoint±ace padding alone would be 95..112; cluster seed must widen.
    seed_s, seed_e = _jev_seed_window(cand, 95.0, 112.0, 200.0)
    assert seed_s <= 85.0  # first - ace pre (5)
    assert seed_e >= 122.0  # last + ace post (12)


def test_build_highlights_flags_off_trim_clip_argv_matches_legacy(monkeypatch, tmp_path):
    """Flags off: build_highlights passes the same trim_clip start/end as cut_window."""
    from ai_video_editor import highlights

    monkeypatch.setattr(settings, "workspace_dir", tmp_path)
    monkeypatch.setattr(settings, "grokbot_jev_clip_cut_enabled", False)
    monkeypatch.setattr(settings, "grokbot_jev_clip_cut_shadow", False)
    monkeypatch.setattr(highlights, "get_duration_seconds", lambda _p: 200.0)
    monkeypatch.setattr(highlights, "_vlm_enabled", lambda: False)

    trim_calls: list[tuple] = []

    def fake_trim(src, dest, start, end, **kw):
        trim_calls.append((src, dest, start, end))
        return True, None

    monkeypatch.setattr(highlights, "trim_clip", fake_trim)

    asset = {
        "id": "v1",
        "path": "rec.mp4",
        "filename": "Valorant_05-15-2026_21-19-8-0.mp4",
    }
    cand = {
        "id": "c1",
        "source": "overwolf_game_event",
        "event_type": "kill",
        "start_seconds": 47.0,
        "end_seconds": 58.0,
        "metadata": {"anchor_seconds": 50.0},
    }
    rankings = [
        {
            "candidate_id": "c1",
            "keep": True,
            "suggested_start_seconds": 47.0,
            "suggested_end_seconds": 58.0,
        }
    ]
    expected = cut_window(cand, rankings[0], 200.0)
    summary = highlights.build_highlights(asset, rankings, [cand])
    assert summary["clips"]
    assert len(trim_calls) == 1
    _src, _dest, start, end = trim_calls[0]
    assert (start, end) == expected
    # index.json also records those windows
    folder = settings.workspace_dir / summary["folder"]
    written = json.loads((folder / "index.json").read_text("utf-8"))
    assert written["clips"][0]["start_seconds"] == expected[0]
    assert written["clips"][0]["end_seconds"] == expected[1]


def test_run_stats_includes_skipped_no_anchor():
    stats = CutRunStats(clips=3, skipped_no_anchor=2, jev_requests=1)
    stats.log_summary()  # must not raise
