"""Song structure detection on a synthetic quiet -> loud -> quiet signal."""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import pytest

from ai_video_editor.music.structure import (
    STRUCTURE_ANALYZER_VERSION,
    detect_structure,
    structure_from_signal,
)

SR = 22050
DURATION_S = 20.0


def synthetic_song() -> np.ndarray:
    """20s tone: quiet 0-6s, loud 6-14s, quiet 14-20s."""
    t = np.arange(int(SR * DURATION_S)) / SR
    amplitude = np.where((t >= 6.0) & (t < 14.0), 0.8, 0.05)
    return (amplitude * np.sin(2 * np.pi * 220.0 * t)).astype(np.float32)


def write_wav(path: Path, y: np.ndarray) -> Path:
    pcm = (np.clip(y, -1.0, 1.0) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SR)
        wav.writeframes(pcm.tobytes())
    return path


def assert_valid(structure) -> None:
    assert structure.rising[0] == 0.0
    assert structure.rising[1] == structure.climax[0]
    assert structure.climax[1] == structure.falling[0]
    assert abs(structure.falling[1] - DURATION_S) < 0.05
    assert structure.climax[0] <= structure.drop_s <= structure.climax[1]


def test_structure_tiles_the_song_and_finds_the_loud_middle() -> None:
    structure = structure_from_signal(synthetic_song(), SR)
    assert_valid(structure)
    assert 4.5 <= structure.climax[0] <= 7.5
    assert 12.5 <= structure.climax[1] <= 15.5
    assert structure.source == "auto"
    assert structure.analyzer_version == STRUCTURE_ANALYZER_VERSION


def test_detect_structure_decodes_a_wav_file(tmp_path) -> None:
    wav = write_wav(tmp_path / "song.wav", synthetic_song())
    assert_valid(detect_structure(wav))


def test_silence_degrades_to_one_climax_span() -> None:
    structure = structure_from_signal(np.zeros(SR * 10, dtype=np.float32), SR)
    assert structure.rising == (0.0, 0.0)
    assert structure.climax == (0.0, 10.0)
    assert structure.falling == (10.0, 10.0)
    assert structure.drop_s == 0.0


@pytest.mark.parametrize("seconds", [0.1, 0.5, 1.5])
@pytest.mark.parametrize("shape", ["steady", "ramp_up", "ramp_down", "silent"])
def test_very_short_tracks_still_tile_without_error(seconds, shape) -> None:
    t = np.arange(int(SR * seconds)) / SR
    envelope = {
        "steady": np.full_like(t, 0.5),
        "ramp_up": t / seconds,
        "ramp_down": 1.0 - t / seconds,
        "silent": np.zeros_like(t),
    }[shape]
    y = (envelope * np.sin(2 * np.pi * 220.0 * t)).astype(np.float32)
    structure = structure_from_signal(y, SR)
    assert structure.rising[0] == 0.0
    assert structure.rising[1] == structure.climax[0]
    assert structure.climax[1] == structure.falling[0]
    duration = y.size / SR
    assert structure.falling[1] == pytest.approx(duration)
    assert structure.climax[0] <= structure.climax[1] <= duration + 1e-9
    assert structure.climax[0] <= structure.drop_s <= structure.climax[1]
