"""ffmpeg frame-sampling helper for VLM input.

Pure planner (`plan_sample_times`) separated from the ffmpeg shell
(`extract_frames`) so the sampling math is unit-testable without
requiring ffmpeg on the CI runner.

Frames land in a per-call temp directory the caller is expected to
clean up (matches the convention in edits.py's per-clip render parts).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from ..config import settings


def plan_sample_times(duration_seconds: float, n_samples: int) -> list[float]:
    """Evenly space N sample points inside the clip's duration.

    Trimmed so the first sample sits at 5% (skipping the intro
    keyframe fade-in Ollama tends to over-index) and the last at 95%
    (dropping the final decode-cut frame). Pure — no ffmpeg call.
    """
    if n_samples <= 0 or duration_seconds <= 0:
        return []
    if n_samples == 1:
        return [duration_seconds * 0.5]
    lead = duration_seconds * 0.05
    span = duration_seconds - 2 * lead
    if span <= 0:
        return [duration_seconds * 0.5]
    step = span / (n_samples - 1)
    return [round(lead + i * step, 3) for i in range(n_samples)]


def extract_frames(
    video_path: str,
    out_dir: Path,
    *,
    n_samples: int,
    duration_seconds: float,
    quality: int = 4,
) -> list[Path]:
    """Extract `n_samples` JPEGs from `video_path` into `out_dir`.

    Returns the actual file paths that were written (drops entries that
    failed to encode). Best-effort — a single failed frame doesn't
    abort the batch. `quality` is ffmpeg's `-q:v` scale (2 = best,
    31 = worst); default 4 balances size + legibility for a VLM.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    times = plan_sample_times(duration_seconds, n_samples)
    written: list[Path] = []
    for i, t in enumerate(times, start=1):
        out_path = out_dir / f"frame_{i:03d}.jpg"
        cmd = [
            settings.ffmpeg_path,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{t:.3f}",
            "-i",
            video_path,
            "-frames:v",
            "1",
            "-q:v",
            str(quality),
            str(out_path),
        ]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=15
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
        if result.returncode == 0 and out_path.is_file():
            written.append(out_path)
    return written
