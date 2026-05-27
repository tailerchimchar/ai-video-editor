"""Audio-peak candidate source.

Cheap, deterministic, no LLM: extract a low-rate mono WAV with ffmpeg,
compute per-window RMS energy with numpy, and treat loud regions as
candidate highlight moments. Source files are never modified.
"""

import shutil
import subprocess
import tempfile
import wave
from pathlib import Path

import numpy as np

from ..config import settings

_SAMPLE_RATE = 8000
_WINDOW_SECONDS = 1.0
_BYTES_PER_SAMPLE = 2  # int16 mono


def _preflight(duration: float) -> None:
    """Bound resource use before extracting a WAV.

    Raises RuntimeError (→ surfaced as a failed job with a clear reason)
    if the recording is too long or there isn't enough free disk to hold
    the temp WAV with a safety margin. This is what stops a single long
    recording from filling the C: drive.
    """
    if duration > settings.analyze_audio_max_seconds:
        raise RuntimeError(
            f"recording is {duration:.0f}s, exceeds "
            f"analyze_audio_max_seconds={settings.analyze_audio_max_seconds:.0f} "
            f"— audio analysis skipped to bound resource use"
        )

    est_wav_bytes = int(_SAMPLE_RATE * _BYTES_PER_SAMPLE * duration)
    margin_bytes = settings.min_free_disk_mb * 1024 * 1024
    free = shutil.disk_usage(tempfile.gettempdir()).free
    if free < est_wav_bytes + margin_bytes:
        raise RuntimeError(
            f"insufficient disk: need ~{(est_wav_bytes + margin_bytes) // (1024 * 1024)} MB "
            f"free (WAV ~{est_wav_bytes // (1024 * 1024)} MB + "
            f"{settings.min_free_disk_mb} MB margin), have "
            f"{free // (1024 * 1024)} MB — audio analysis skipped"
        )


def _extract_mono_wav(video_path: str, out_path: str) -> bool:
    cmd = [
        settings.ffmpeg_path,
        "-y",
        "-i",
        video_path,
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(_SAMPLE_RATE),
        "-f",
        "wav",
        out_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode == 0


def energy_curve(video_path: str, duration: float) -> np.ndarray | None:
    """Per-second RMS energy of the recording's audio (or None).

    The single expensive step (ffmpeg WAV extract + numpy RMS), shared by
    the audio-peak source and the Riot-offset calibrator so we decode the
    audio only once. Raises RuntimeError via _preflight if unsafe.
    """
    _preflight(duration)

    tmp = Path(tempfile.gettempdir()) / f"avp_{Path(video_path).stem}.wav"
    try:
        if not _extract_mono_wav(video_path, str(tmp)):
            return None
        with wave.open(str(tmp), "rb") as wav:
            raw = wav.readframes(wav.getnframes())
        if not raw:
            return None
        samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
        win = int(_SAMPLE_RATE * _WINDOW_SECONDS)
        if win == 0 or samples.size < win:
            return None
        usable = (samples.size // win) * win
        return np.sqrt(np.mean(samples[:usable].reshape(-1, win) ** 2, axis=1))
    finally:
        tmp.unlink(missing_ok=True)


def peaks_from_energy(rms: np.ndarray | None) -> list[dict]:
    """Loud-region candidate windows from a per-second RMS curve.

    Each item: {start_seconds, end_seconds, confidence} where confidence
    is the window's RMS normalized to the loudest window (0..1).
    """
    if rms is None or rms.size == 0:
        return []
    peak = float(rms.max())
    if peak <= 0:
        return []

    norm = rms / peak
    threshold = settings.analyze_peak_threshold
    pad = settings.analyze_window_padding
    total_seconds = rms.size * _WINDOW_SECONDS

    loud_idx = np.where(norm >= threshold)[0]
    if loud_idx.size == 0:
        return []

    candidates: list[dict] = []
    group_start = loud_idx[0]
    prev = loud_idx[0]
    group_max = norm[loud_idx[0]]

    def _emit(g_start: int, g_end: int, conf: float) -> None:
        start = max(0.0, g_start * _WINDOW_SECONDS - pad)
        end = min(total_seconds, (g_end + 1) * _WINDOW_SECONDS + pad)
        candidates.append(
            {
                "start_seconds": round(start, 2),
                "end_seconds": round(end, 2),
                "confidence": round(float(conf), 3),
            }
        )

    for idx in loud_idx[1:]:
        if idx - prev <= pad:
            group_max = max(group_max, norm[idx])
        else:
            _emit(group_start, prev, group_max)
            group_start = idx
            group_max = norm[idx]
        prev = idx
    _emit(group_start, prev, group_max)

    candidates.sort(key=lambda c: c["confidence"], reverse=True)
    return candidates[: settings.analyze_max_candidates]


def detect_audio_peaks(video_path: str, duration: float) -> list[dict]:
    """Loud-region candidates (decodes audio, then `peaks_from_energy`)."""
    return peaks_from_energy(energy_curve(video_path, duration))
