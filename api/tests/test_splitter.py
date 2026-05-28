"""Pure-function tests for the multi-game VOD splitter.

The ffmpeg subprocess calls are NOT exercised here — those are
integration paths that the user smokes via the API on a real file.
What we test are the deterministic transforms:

  ffmpeg stderr text → BlackInterval[] → GameSegment[]
"""

import pytest

from ai_video_editor.splitter import (
    BlackInterval,
    child_filename,
    intervals_to_segments,
    parse_blackdetect_output,
)


# ----- parse_blackdetect_output -----


def test_parse_extracts_all_intervals():
    """Real ffmpeg output has surrounding noise lines (banner, codec
    info, progress); the regex must skip those and only pull the
    `blackdetect @ ...` lines."""
    stderr = """
ffmpeg version 8.1.1 Copyright (c) 2000-2025 the FFmpeg developers
[h264 @ 0x...] decode error
Stream #0:0: Video: h264
[blackdetect @ 0x7fff] black_start:100.5 black_end:103.7 black_duration:3.2
some other log line
[blackdetect @ 0x7fff] black_start:1234.0 black_end:1240.5 black_duration:6.5
frame= 12345 fps=60
[blackdetect @ 0x7fff] black_start:3000.1 black_end:3002.8 black_duration:2.7
"""
    out = parse_blackdetect_output(stderr)
    assert len(out) == 3
    assert out[0].start == 100.5 and out[0].end == 103.7
    assert out[1].start == 1234.0 and out[1].end == 1240.5
    assert out[2].start == 3000.1 and out[2].end == 3002.8


def test_parse_handles_empty_stderr():
    assert parse_blackdetect_output("") == []


def test_parse_handles_no_black_lines():
    """Some VODs have no black periods at all (e.g. one short clip)."""
    assert parse_blackdetect_output("just a normal ffmpeg run\nframe=1234\n") == []


def test_blackinterval_computes_duration_and_midpoint():
    interval = BlackInterval(start=100.0, end=104.0)
    assert interval.duration == 4.0
    assert interval.midpoint == 102.0


# ----- intervals_to_segments -----


def test_segments_three_game_vod():
    """The classic case: a 1.5hr scrim with 2 black transitions between
    3 games. Should produce 3 segments."""
    intervals = [
        BlackInterval(start=2000.0, end=2010.0),  # midpoint 2005
        BlackInterval(start=4000.0, end=4010.0),  # midpoint 4005
    ]
    segments = intervals_to_segments(intervals, duration=6000.0)
    assert len(segments) == 3
    # Indexes 1-based, contiguous
    assert [s.index for s in segments] == [1, 2, 3]
    # Boundaries at midpoints
    assert segments[0].start == 0.0 and segments[0].end == 2005.0
    assert segments[1].start == 2005.0 and segments[1].end == 4005.0
    assert segments[2].start == 4005.0 and segments[2].end == 6000.0


def test_segments_drops_too_short_pieces():
    """A 30-second segment between two black intervals is almost certainly
    a UI artifact (a flash during a teamfight), not a real game."""
    intervals = [
        BlackInterval(start=100.0, end=102.0),  # midpoint 101
        BlackInterval(start=130.0, end=132.0),  # midpoint 131 (gap=30s)
        BlackInterval(start=3000.0, end=3005.0),  # midpoint 3002.5
    ]
    segments = intervals_to_segments(intervals, duration=6000.0)
    # The 30-second middle gets dropped (101 → 131 = 30s, below the
    # 60s floor). Indexes renumber so we don't have a hole.
    assert [s.index for s in segments] == [1, 2, 3]
    assert len(segments) == 3
    # First segment: 0 → 101 (101s — long enough)
    assert segments[0].end == 101.0
    # The 30s gap was dropped; segment 2 starts where the 30s gap would
    # have ended, at the next valid cut.
    assert segments[1].start == 131.0
    assert segments[1].end == 3002.5
    # Tail segment
    assert segments[2].start == 3002.5
    assert segments[2].end == 6000.0


def test_segments_no_intervals_returns_one_segment():
    """A clean VOD with no detected blacks = one game = one segment."""
    segments = intervals_to_segments([], duration=2400.0)
    assert len(segments) == 1
    assert segments[0].start == 0.0
    assert segments[0].end == 2400.0


def test_segments_handles_zero_duration():
    assert intervals_to_segments([], duration=0.0) == []


def test_segments_filters_intervals_outside_duration():
    """An interval that ffmpeg detected past the end of the VOD (rare,
    but possible if duration was probed slightly low) shouldn't break."""
    intervals = [
        BlackInterval(start=100.0, end=102.0),
        BlackInterval(start=9999.0, end=10001.0),  # past EOF
    ]
    segments = intervals_to_segments(intervals, duration=200.0)
    # Only the in-bounds black splits the file → 2 segments expected,
    # but neither side is < 60s, so we get [0..101] (segment dropped, 101s
    # is OK) and [101..200] (99s — also kept).
    assert len(segments) == 2


def test_segments_with_custom_min_length():
    """Caller can set a more permissive floor for smaller VODs."""
    intervals = [BlackInterval(start=20.0, end=22.0)]  # midpoint 21
    # Default 60s floor would drop everything; with a 5s floor we get 2 segs.
    segments = intervals_to_segments(intervals, duration=100.0, min_segment_length=5.0)
    assert len(segments) == 2
    assert segments[0].end == 21.0
    assert segments[1].start == 21.0


# ----- child_filename -----


def test_child_filename_appends_game_index():
    from ai_video_editor.splitter import GameSegment

    seg2 = GameSegment(start=0.0, end=100.0, index=2)
    assert child_filename("scrim_2026-05-26.mp4", seg2) == "scrim_2026-05-26_game2.mp4"


def test_child_filename_preserves_unusual_extension():
    from ai_video_editor.splitter import GameSegment

    seg1 = GameSegment(start=0.0, end=100.0, index=1)
    assert child_filename("recording.mkv", seg1) == "recording_game1.mkv"


@pytest.mark.parametrize(
    "parent,index,expected",
    [
        ("a.mp4", 1, "a_game1.mp4"),
        ("a.b.c.mp4", 3, "a.b.c_game3.mp4"),
        ("noext", 1, "noext_game1.mp4"),  # default extension when missing
    ],
)
def test_child_filename_edge_cases(parent, index, expected):
    from ai_video_editor.splitter import GameSegment

    seg = GameSegment(start=0.0, end=100.0, index=index)
    assert child_filename(parent, seg) == expected
