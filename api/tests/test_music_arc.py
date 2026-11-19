"""Arc planner tests: pure functions on synthetic clips and a fake song."""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_video_editor.edit_plan import validate_plan
from ai_video_editor.intensity import ClipWithIntensity
from ai_video_editor.music.analysis import SongAnalysis
from ai_video_editor.music.arc import plan_arc, trim_to_peak
from ai_video_editor.music.beats import BeatTrack
from ai_video_editor.music.structure import SongStructure


def _analysis(drop_s: float = 22.0, duration: float = 60.0) -> SongAnalysis:
    structure = SongStructure(
        rising=(0.0, 20.0),
        climax=(20.0, 30.0),
        falling=(30.0, duration),
        drop_s=drop_s,
        source="auto",
        analyzer_version="v1",
    )
    beats = BeatTrack(
        mp3_path=Path("song.mp3"),
        tempo_bpm=120.0,
        beats=tuple(i * 0.5 for i in range(int(duration * 2))),
        detector="fake",
    )
    return SongAnalysis(beats=beats, structure=structure, file_sha256="abc")


def _clip(ref: str, start: float, intensity: float, topic: str | None = None, length=5.0):
    return ClipWithIntensity(
        clip_ref=ref, start=start, end=start + length, intensity=intensity, topic=topic
    )


def _six_clips() -> list[ClipWithIntensity]:
    # Chronological order is deliberately not intensity order.
    intensities = [0.5, 0.9, 0.2, 0.7, 0.3, 0.6]
    return [_clip(f"c{i}", 100.0 + i * 10, v, "teamfight") for i, v in enumerate(intensities)]


def _window(plan, ref: str) -> tuple[float, float]:
    t = 0.0
    for r in plan.order:
        clip = next(c for c in plan.source_clips if c.clip_ref == r)
        if r == ref:
            return t, t + (clip.end - clip.start)
        t += clip.end - clip.start
    raise AssertionError(ref)


def _drop_in_cut(plan, analysis: SongAnalysis) -> float:
    return analysis.structure.drop_s - plan.music.trim_start_s


# --- trim_to_peak ------------------------------------------------------


def test_trim_puts_drop_at_peak_position() -> None:
    start, end = trim_to_peak(_analysis(drop_s=22.0), peak_at_s=12.0, target_seconds=30.0)
    assert (start, end) == (10.0, 40.0)


def test_trim_clamps_to_song_start() -> None:
    start, end = trim_to_peak(_analysis(drop_s=5.0), peak_at_s=12.0, target_seconds=30.0)
    assert (start, end) == (0.0, 30.0)


def test_trim_clamps_to_song_end() -> None:
    start, end = trim_to_peak(_analysis(drop_s=55.0, duration=60.0), 5.0, 30.0)
    assert (start, end) == (30.0, 60.0)


def test_trim_short_song_ends_at_song_end() -> None:
    start, end = trim_to_peak(_analysis(drop_s=10.0, duration=20.0), 5.0, 30.0)
    assert (start, end) == (0.0, 20.0)


# --- arc_montage -------------------------------------------------------


def test_arc_best_clip_window_contains_drop() -> None:
    analysis = _analysis()
    plan = plan_arc(_six_clips(), analysis, "arc_montage", 30.0)
    lo, hi = _window(plan, "c1")  # 0.9 is the best clip
    assert lo <= _drop_in_cut(plan, analysis) <= hi


def test_arc_rising_ascends_and_falling_holds_remainder() -> None:
    plan = plan_arc(_six_clips(), _analysis(), "arc_montage", 30.0)
    by_ref = {c.clip_ref: c.intensity for c in plan.source_clips}
    best_at = plan.order.index("c1")
    rising = [by_ref[r] for r in plan.order[:best_at]]
    assert rising == sorted(rising)
    assert len(plan.order[best_at + 1 :]) >= 1
    assert sorted(plan.order) == sorted(by_ref)


def test_arc_total_within_five_percent_of_target() -> None:
    clips = [_clip(f"c{i}", i * 10.0, 0.1 * (i + 1), length=4.0 + i % 3) for i in range(10)]
    plan = plan_arc(clips, _analysis(), "arc_montage", 30.0)
    total = sum(c.end - c.start for c in plan.source_clips)
    assert 28.5 <= total <= 31.5


def test_arc_plan_is_valid_and_records_trim() -> None:
    plan = plan_arc(_six_clips(), _analysis(), "arc_montage", 30.0)
    assert validate_plan(plan, known_sounds=()) == []
    assert plan.format == "arc_montage"
    assert plan.music is not None and plan.music.trim_to_peak is True
    assert plan.music.trim_end_s - plan.music.trim_start_s == pytest.approx(30.0)


def test_arc_never_puts_one_or_two_kill_on_the_drop() -> None:
    clips = _six_clips()
    clips[1] = _clip("c1", 110.0, 0.99, "multi_kill_2")  # would be best
    clips[3] = _clip("c3", 130.0, 0.95, "kill")
    analysis = _analysis()
    plan = plan_arc(clips, analysis, "arc_montage", 30.0)
    drop_t = _drop_in_cut(plan, analysis)
    lo, hi = _window(plan, "c5")  # best eligible clip is 0.6
    assert lo <= drop_t <= hi
    for ref in ("c1", "c3"):
        if ref in plan.order:
            a, b = _window(plan, ref)
            assert not (a <= drop_t <= b)


def test_arc_with_only_small_kills_cannot_fill_the_drop() -> None:
    clips = [_clip("a", 0.0, 0.9, "kill"), _clip("b", 10.0, 0.8, "multi_kill_2")]
    with pytest.raises(ValueError, match="drop"):
        plan_arc(clips, _analysis(), "arc_montage", 10.0)


# --- story -------------------------------------------------------------


def test_story_is_chronological_and_only_music_moves() -> None:
    clips = _six_clips()
    plan = plan_arc(list(reversed(clips)), _analysis(), "story", 30.0)
    assert plan.format == "story_short"
    assert plan.order == [c.clip_ref for c in clips]
    assert plan.music.trim_to_peak is True
    assert plan.music.trim_start_s is not None
    assert validate_plan(plan, known_sounds=()) == []
