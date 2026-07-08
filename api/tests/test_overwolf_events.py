"""Tests for the overwolf log parser + candidate source.

Uses a synthetic log file (temp dir) to test parsing without touching
the user's real Overwolf install.
"""

from __future__ import annotations

from pathlib import Path

from ai_video_editor.candidates.overwolf_events import detect_overwolf_events
from ai_video_editor.overwolf_log import (
    clear_dispatch_cache,
    dispatched_video_for,
    events_for_video,
)


def _write_log(tmp_path: Path, lines: list[str]) -> Path:
    """Create a fake Outplayed background log file."""
    logf = tmp_path / "background.html.1.log"
    logf.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return logf


DISPATCH_LINE_TEMPLATE = (
    '2026-07-01 00:42:20,447 (INFO) </background.js> (:42500) - '
    '[StreamingCaptureService] Dispatch new video created '
    '{{"format":"video","type":"fullMatch","url":"overwolf://media/videos/x/y.mp4",'
    '"path":"{path}","duration":{duration_ms},'
    '"startTime":{start_time_ms},"events":[{events}],'
    '"shouldOverrideLastMedia":false,"audioTrackMap":[]}}'
)


def _make_event(etype: str, time_ms: int, data: str = "") -> str:
    if data:
        return (
            f'{{"type":"{etype}","time":{time_ms},'
            f'"timing":{{"past":15000,"future":5000}},"data":"{data}"}}'
        )
    return (
        f'{{"type":"{etype}","time":{time_ms},'
        f'"timing":{{"past":15000,"future":5000}}}}'
    )


def _make_dispatch(
    path: str,
    duration_ms: int = 2000000,
    start_time_ms: int = 1782882412000,
    events: list[str] | None = None,
) -> str:
    events = events or []
    events_json = ",".join(events)
    escaped_path = path.replace("\\", "\\\\")
    return DISPATCH_LINE_TEMPLATE.format(
        path=escaped_path,
        duration_ms=duration_ms,
        start_time_ms=start_time_ms,
        events=events_json,
    )


def test_dispatched_video_for_returns_none_when_no_log_dir(tmp_path) -> None:
    """Missing log dir returns None (Outplayed not installed)."""
    clear_dispatch_cache()
    result = dispatched_video_for(
        "/tmp/x.mp4", log_dir=tmp_path / "nonexistent"
    )
    assert result is None


def test_dispatched_video_returns_none_for_unlisted_file(tmp_path) -> None:
    """A file not mentioned in any log → None (user-clipped short)."""
    clear_dispatch_cache()
    _write_log(tmp_path, [_make_dispatch(r"C:\path\to\other.mp4")])
    result = dispatched_video_for(
        r"C:\path\to\different_file.mp4", log_dir=tmp_path
    )
    assert result is None


def test_dispatched_video_parses_events(tmp_path) -> None:
    """Happy path: log has a dispatch line with kills → we return the
    events with correct offsets."""
    clear_dispatch_cache()
    target = r"C:\Videos\Val\match_1.mp4"
    events = [
        _make_event("kill", 125000, "1"),
        _make_event("headshot", 125000, "1"),
        _make_event("death", 200000, "1"),
        _make_event("kill", 300000, "2"),
        _make_event("spike_defused", 400000),
        _make_event("victory", 500000),
    ]
    _write_log(tmp_path, [_make_dispatch(target, events=events)])
    dv = dispatched_video_for(target, log_dir=tmp_path)
    assert dv is not None
    assert dv.output_type == "fullMatch"
    assert dv.duration_ms == 2000000
    assert len(dv.events) == 6
    kills = [e for e in dv.events if e["type"] == "kill"]
    assert len(kills) == 2
    assert kills[0]["time_ms_in_video"] == 125000
    assert kills[0]["data"] == "1"


def test_path_lookup_case_insensitive(tmp_path) -> None:
    """Windows path lookups shouldn't miss on casing or separator style."""
    clear_dispatch_cache()
    stored = r"C:\Videos\Val\Match_1.mp4"
    _write_log(tmp_path, [_make_dispatch(stored)])
    # Lookup with all lowercase + forward slashes
    result = dispatched_video_for(
        "c:/videos/val/match_1.mp4", log_dir=tmp_path
    )
    assert result is not None


def test_events_for_video_filters_by_type(tmp_path) -> None:
    """`event_types=(...)` restricts what's returned."""
    clear_dispatch_cache()
    target = r"C:\match.mp4"
    events = [
        _make_event("kill", 100000, "1"),
        _make_event("death", 200000, "1"),
        _make_event("assist", 300000, "1"),
    ]
    _write_log(tmp_path, [_make_dispatch(target, events=events)])
    kills_only = events_for_video(
        target, log_dir=tmp_path, event_types=("kill",)
    )
    assert kills_only is not None
    assert len(kills_only) == 1
    assert kills_only[0]["type"] == "kill"


# ---------------------------------------------------------------------
# Candidate source
# ---------------------------------------------------------------------


def test_detect_returns_empty_when_video_not_dispatched(
    tmp_path, monkeypatch
) -> None:
    """User-clipped short (no dispatch entry) → empty list, other
    sources cover it."""
    clear_dispatch_cache()
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    # Set up the expected subfolder structure (empty)
    (tmp_path / "Overwolf" / "Log" / "Apps" / "Outplayed").mkdir(
        parents=True
    )
    result = detect_overwolf_events(
        "/tmp/nonexistent.mp4", duration_seconds=60.0
    )
    assert result == []


def test_detect_emits_one_candidate_per_surfaced_event(
    tmp_path, monkeypatch
) -> None:
    """Kill / death / assist / spike / victory all emit candidates.
    headshot piggybacks on the matching kill via metadata, doesn't
    double-emit."""
    clear_dispatch_cache()
    log_dir = tmp_path / "Overwolf" / "Log" / "Apps" / "Outplayed"
    log_dir.mkdir(parents=True)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    target = r"C:\Val\match.mp4"
    events = [
        _make_event("kill", 125000, "1"),
        _make_event("headshot", 125000, "1"),  # deduped into kill's metadata
        _make_event("death", 200000, "1"),
        _make_event("assist", 300000, "1"),
        _make_event("spike_defused", 400000),
        _make_event("victory", 500000),
    ]
    _write_log(log_dir, [_make_dispatch(target, events=events)])

    candidates = detect_overwolf_events(target, duration_seconds=600.0)
    # Kill, death, assist, spike_defused, victory = 5 candidates.
    # Headshot NOT a separate candidate — it's a flag on the kill.
    assert len(candidates) == 5
    event_types = [c["event_type"] for c in candidates]
    assert event_types == ["kill", "death", "assist", "objective", "victory"]
    kill = candidates[0]
    assert kill["confidence"] == 1.0
    # Anchor is time_ms/1000 - 3s offset (killfeed lag correction)
    assert kill["metadata"]["anchor_seconds"] == 122.0
    assert kill["metadata"]["headshot"] is True
    assert kill["metadata"]["overwolf_data"] == "1"


def test_detect_clamps_windows_to_video_bounds(
    tmp_path, monkeypatch
) -> None:
    """Anchor at t=0 shouldn't produce start_seconds=-5. Anchor near
    the end shouldn't produce end_seconds past duration."""
    clear_dispatch_cache()
    log_dir = tmp_path / "Overwolf" / "Log" / "Apps" / "Outplayed"
    log_dir.mkdir(parents=True)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    target = r"C:\Val\match.mp4"
    events = [
        _make_event("kill", 500, "1"),        # 0.5s into video
        _make_event("kill", 59500, "2"),      # 59.5s (near 60s end)
    ]
    _write_log(log_dir, [_make_dispatch(target, events=events)])
    candidates = detect_overwolf_events(target, duration_seconds=60.0)
    assert candidates[0]["start_seconds"] >= 0.0
    assert candidates[1]["end_seconds"] <= 60.0


def test_detect_skips_events_past_duration(tmp_path, monkeypatch) -> None:
    """Log occasionally has events slightly past the video's actual
    duration (encoder rounding). Drop them cleanly."""
    clear_dispatch_cache()
    log_dir = tmp_path / "Overwolf" / "Log" / "Apps" / "Outplayed"
    log_dir.mkdir(parents=True)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    target = r"C:\Val\match.mp4"
    events = [
        _make_event("kill", 10000, "1"),     # 10s — kept (7s after offset)
        _make_event("kill", 120000, "2"),    # 120s — way past 60s, dropped
    ]
    _write_log(log_dir, [_make_dispatch(target, events=events)])
    candidates = detect_overwolf_events(target, duration_seconds=60.0)
    assert len(candidates) == 1
    # Anchor = 10s - 3s offset = 7s
    assert candidates[0]["metadata"]["anchor_seconds"] == 7.0
