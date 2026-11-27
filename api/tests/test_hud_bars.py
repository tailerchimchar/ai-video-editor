"""League HUD HP / mana bar reader: synthetic frames, plus one real-recording
smoke test that is skipped when the file is missing."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from ai_video_editor.league.hud_bars import (
    HP_BOX,
    MANA_BOX,
    BarSample,
    drops,
    read_bars,
    sample_bars,
    strip_box,
)

GREEN = (5, 150, 12)
BLUE = (20, 110, 210)
DARK = (2, 12, 8)
WHITE = (240, 240, 240)


def _px(box, w, h):
    x0, y0, x1, y1 = box
    return round(w * x0), round(h * y0), round(w * x1), round(h * y1)


def _frame(hp=1.0, mana=1.0, w=1280, h=720, text=False):
    f = np.zeros((h, w, 3), np.uint8)
    for box, colour, fill in ((HP_BOX, GREEN, hp), (MANA_BOX, BLUE, mana)):
        x0, y0, x1, y1 = _px(box, w, h)
        f[y0:y1, x0:x1] = DARK
        n = round((x1 - x0) * fill)
        f[y0:y1, x0 : x0 + n] = colour
        if text:  # "867 / 1090" over the middle, "+1.2" right of the fill
            mid = (x0 + x1) // 2
            f[y0 + 1 : y1 - 1, mid - 20 : mid + 20 : 3] = WHITE
            f[y0 + 1 : y1 - 1, x1 - 12 : x1 - 2 : 2] = WHITE
    return f


def _strip(frame):
    h, w = frame.shape[:2]
    x0, y0, x1, y1 = _px(strip_box(), w, h)
    return frame[y0:y1, x0:x1]


@pytest.mark.parametrize("size", [(1280, 720), (1920, 1080)])
def test_full_bars(size):
    hp, mana = read_bars(_strip(_frame(1.0, 1.0, *size)))
    assert hp == pytest.approx(1.0, abs=0.01)
    assert mana == pytest.approx(1.0, abs=0.01)


@pytest.mark.parametrize("size", [(1280, 720), (1920, 1080)])
def test_sixty_percent_with_text(size):
    hp, mana = read_bars(_strip(_frame(0.6, 0.35, *size, text=True)))
    assert hp == pytest.approx(0.6, abs=0.015)
    assert mana == pytest.approx(0.35, abs=0.015)


def test_dead_champion_reads_none_not_zero_drop():
    assert read_bars(_strip(_frame(0.0, 0.0))) == (None, None)


def test_non_league_frame_is_unreadable():
    rng = np.random.default_rng(1)
    noise = rng.integers(0, 255, size=(720, 1280, 3), dtype=np.uint8)
    assert read_bars(_strip(noise)) == (None, None)
    grey = np.full((720, 1280, 3), 128, np.uint8)
    assert read_bars(_strip(grey)) == (None, None)


def test_green_only_on_the_right_is_not_a_bar():
    f = _frame(0.0, 0.0)
    _x0, y0, x1, y1 = _px(HP_BOX, 1280, 720)
    f[y0:y1, x1 - 30 : x1] = GREEN
    assert read_bars(_strip(f))[0] is None


def test_empty_or_bad_strip_is_unreadable():
    assert read_bars(np.zeros((0, 0, 3), np.uint8)) == (None, None)
    assert read_bars(None) == (None, None)


def test_sample_bars_uses_reader_once_per_time():
    seen = []

    def reader(path, t, size):
        seen.append((path, t, size))
        return _strip(_frame(0.5 if t > 10 else 1.0, 1.0))

    out = sample_bars("v.mp4", [5.0, 15.0], reader=reader, probe=lambda p: (1280, 720))
    assert [s.t_rec_s for s in out] == [5.0, 15.0]
    assert out[0].hp == pytest.approx(1.0, abs=0.01)
    assert out[1].hp == pytest.approx(0.5, abs=0.01)
    assert seen == [("v.mp4", 5.0, (1280, 720)), ("v.mp4", 15.0, (1280, 720))]


def test_sample_bars_unknown_size_is_all_none():
    out = sample_bars("v.mp4", [1.0, 2.0], reader=lambda *a: None, probe=lambda p: None)
    assert out == [BarSample(t_rec_s=1.0, hp=None, mana=None), BarSample(t_rec_s=2.0)]


def test_sample_bars_reader_failure_is_none():
    out = sample_bars("v.mp4", [1.0], reader=lambda *a: None, probe=lambda p: (1280, 720))
    assert out == [BarSample(t_rec_s=1.0)]


def _s(t, hp=None, mana=None):
    return BarSample(t_rec_s=t, hp=hp, mana=mana)


def test_drops_consecutive_readable_pairs_and_rises_ignored():
    samples = [_s(0, 1.0), _s(5, 0.7), _s(10, None), _s(15, 0.4), _s(20, 1.0), _s(25, 0.98)]
    assert drops(samples, "hp", 0.2) == [(5, pytest.approx(0.3)), (15, pytest.approx(0.3))]
    assert drops(samples, "hp", 0.01) == [
        (5, pytest.approx(0.3)),
        (15, pytest.approx(0.3)),
        (25, pytest.approx(0.02)),
    ]


def test_drops_by_key_and_empty():
    assert drops([_s(0, 1, 1.0), _s(5, 1, 0.9)], "mana", 0.05) == [(5, pytest.approx(0.1))]
    assert drops([], "hp", 0.1) == []


RECORDINGS = Path("C:/Users/taile/Videos/Overwolf/Outplayed/League of Legends")
YUNARA = (
    RECORDINGS
    / "League of Legends_11-25-2026_23-59-57-247"
    / "League of Legends_11-26-2026_0-15-34-450.mp4"
)
JHIN = (
    RECORDINGS
    / "League of Legends_11-20-2026_20-1-10-276"
    / "League of Legends_11-20-2026_20-35-21-0.mp4"
)


@pytest.mark.skipif(not (YUNARA.exists() and JHIN.exists()), reason="no real recordings")
def test_real_recordings_smoke():
    (y,) = sample_bars(str(YUNARA), [400.0])  # 867 / 1090 HP, full mana
    assert y.hp == pytest.approx(0.80, abs=0.02)
    assert y.mana == pytest.approx(1.0, abs=0.02)
    alive, dead = sample_bars(str(JHIN), [30.0, 400.0])  # 665 / 870, 244 / 300; then dead
    assert alive.hp == pytest.approx(0.76, abs=0.02)
    assert alive.mana == pytest.approx(0.81, abs=0.02)
    assert (dead.hp, dead.mana) == (None, None)
