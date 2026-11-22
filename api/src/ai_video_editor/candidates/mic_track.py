"""Find and transcribe the owner's microphone track (spec 4.1, R1).

Outplayed files carry several audio streams (game, comms, often the owner
mic) but stream position is not reliable. The mic is found by, in order:
override setting, stream title/handler label, speech activity, else not at
all (fail closed: a wrong pick would label teammates as the owner).
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ..config import settings
from .sentiment import score_sentiment
from .transcribe_backends import _extract_audio, select_backend

_log = logging.getLogger(__name__)

_LABEL = re.compile(r"(?i)mic|microphone|voice|input|commentary")
_RATIO = 1.5  # winner must beat the runner-up by this factor
_RATE = 16000
_FRAME = 320  # 20 ms at 16 kHz
# Activity is level independent: each stream's frame RMS is divided by that
# stream's own 95th-percentile RMS, and a frame is active above this fraction
# (about -12 dB under the stream's loud frames). Below _RMS_FLOOR (int16 units)
# a stream is digital silence and scores 0.
_RMS_PERCENTILE = 95
_REL_THRESHOLD = 0.25
_RMS_FLOOR = 1.0
_SAMPLE_SECONDS = 20


@dataclass(frozen=True)
class AudioStream:
    index: int  # position among audio streams (the N in 0:a:N)
    codec: str
    channels: int
    title: str | None
    handler: str | None


def _ffprobe() -> str:
    ff = settings.ffmpeg_path
    name = Path(ff).name
    if name.lower().startswith("ffmpeg"):
        return str(Path(ff).with_name("ffprobe" + name[len("ffmpeg") :]))
    return "ffprobe"


def probe_audio_streams(path: str) -> list[AudioStream]:
    cmd = [_ffprobe(), "-v", "error", "-select_streams", "a", "-show_streams", "-of", "json", path]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            return []
        data = json.loads(r.stdout or "{}")
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        _log.warning("mic: ffprobe failed: %s", exc)
        return []
    out = []
    for i, s in enumerate(data.get("streams") or []):
        tags = {k.lower(): v for k, v in (s.get("tags") or {}).items()}
        out.append(
            AudioStream(
                index=i,
                codec=s.get("codec_name") or "",
                channels=int(s.get("channels") or 0),
                title=tags.get("title"),
                handler=tags.get("handler_name"),
            )
        )
    return out


def _probe_duration(path: str) -> float:
    cmd = [_ffprobe(), "-v", "error", "-show_entries", "format=duration", "-of", "json", path]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return float(json.loads(r.stdout)["format"]["duration"])
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        return 0.0


def speech_activity(
    path: str,
    index: int,
    *,
    windows: tuple[float, ...] = (0.1, 0.5, 0.8),
    duration: float | None = None,
) -> float:
    """Fraction of 20 ms frames whose RMS is at least `_REL_THRESHOLD` of the
    stream's own 95th-percentile frame RMS, across three 20 s samples, so a
    very quiet mic with speech bursts scores like a loud one.

    `duration` (seconds) avoids a second ffprobe call when the caller knows it."""
    if duration is None:
        duration = _probe_duration(path)
    if duration <= 0:
        return 0.0
    chunks: list[np.ndarray] = []
    for frac in windows:
        start = max(0.0, min(duration * frac, duration - _SAMPLE_SECONDS))
        cmd = [
            settings.ffmpeg_path, "-v", "error", "-ss", f"{start:.2f}", "-t", str(_SAMPLE_SECONDS),
            "-i", path, "-map", f"0:a:{index}", "-ac", "1", "-ar", str(_RATE),
            "-f", "s16le", "-",
        ]  # fmt: skip
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=120)
        except (OSError, subprocess.SubprocessError) as exc:
            _log.warning("mic: activity sample failed: %s", exc)
            continue
        if r.returncode != 0 or not r.stdout:
            continue
        samples = np.frombuffer(r.stdout[: len(r.stdout) // 2 * 2], dtype=np.int16)
        n = len(samples) // _FRAME
        if n == 0:
            continue
        frames = samples[: n * _FRAME].astype(np.float64).reshape(n, _FRAME)
        chunks.append(np.sqrt((frames**2).mean(axis=1)))
    if not chunks:
        return 0.0
    rms = np.concatenate(chunks)
    ref = float(np.percentile(rms, _RMS_PERCENTILE))
    if ref < _RMS_FLOOR:
        return 0.0
    return float((rms / ref >= _REL_THRESHOLD).mean())


def pick_mic_stream(
    streams: list[AudioStream], activity: dict[int, float], *, min_activity: float
) -> tuple[int | None, str]:
    override = settings.mic_stream_override
    if override is not None:
        # Never defeats the single-stream fallback; an out-of-range index is
        # not trusted either (fail closed).
        if len(streams) >= 2 and 0 <= override < len(streams):
            return override, "override"
        return None, "none"
    for s in streams:
        if _LABEL.search(s.title or "") or _LABEL.search(s.handler or ""):
            return s.index, "label"
    if len(streams) < 2:
        return None, "none"
    ranked = sorted(((activity.get(s.index, 0.0), s.index) for s in streams), reverse=True)
    (top, idx), (runner, _) = ranked[0], ranked[1]
    if top >= min_activity and top >= _RATIO * runner:
        return idx, "activity"
    return None, "none"


def transcribe_mic(asset: dict, duration: float) -> tuple[list[dict], dict]:
    """Transcribe the owner mic stream. Returns (segments, meta); segments is
    empty (and meta.method == 'none') when no mic stream can be trusted."""
    path = asset["path"]
    streams = probe_audio_streams(path)
    activity: dict[int, float] = {}
    labelled = any(_LABEL.search((s.title or "") + " " + (s.handler or "")) for s in streams)
    if settings.mic_stream_override is None and not labelled and len(streams) >= 2:
        activity = {s.index: speech_activity(path, s.index, duration=duration) for s in streams}
    index, method = pick_mic_stream(streams, activity, min_activity=settings.mic_min_activity)
    meta = {"stream_index": index, "method": method, "activity": activity}

    segments: list[dict] = []
    if index is not None:
        if duration <= 0 or duration > settings.transcribe_max_seconds:
            meta["error"] = "duration over cap or unknown"
        else:
            with tempfile.TemporaryDirectory(prefix="mic_stt_") as tmp:
                wav = Path(tmp) / "mic.wav"
                if not _extract_audio(path, wav, stream_index=index):
                    meta["error"] = "audio extract failed"
                else:
                    try:
                        raw = select_backend().transcribe_raw(str(wav))
                    except Exception as exc:  # backend failure must not erase prior rows
                        _log.warning("mic: backend failed: %s", exc)
                        meta["error"] = f"backend failed: {exc!s}"[:200]
                        raw = []
                    segments = [
                        {
                            "start_seconds": s.get("start_seconds"),
                            "end_seconds": s.get("end_seconds"),
                            "text": s["text"].strip(),
                            "sentiment_score": score_sentiment(s["text"].strip()),
                            "words": s.get("words") or [],
                        }
                        for s in raw
                        if (s.get("text") or "").strip()
                    ]
    out = Path(settings.workspace_dir) / "mic" / f"{asset['id']}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(meta), encoding="utf-8")
    return segments, meta
