"""Pluggable transcription backends.

Mirrors the VLM backend pattern (`vlm/backends/`). One `TranscribeBackend`
Protocol, two implementations today:

- **`local_whisper`** -- faster-whisper singleton on CUDA or CPU. Private
  by design; audio never leaves the machine. Default.
- **`groq`** -- Groq's OpenAI-compatible audio endpoint using
  `whisper-large-v3-turbo`. ~$0.02 per 40-min VOD, ~30s wall-clock. The
  cloud path for the SaaS pilot.

Each backend returns the SAME raw-segment shape:

```
[
    {
        "start_seconds": 12.3,
        "end_seconds": 14.9,
        "text": "let's go",
        "words": [
            {"word": "let's", "start": 12.3, "end": 12.7},
            {"word": "go",    "start": 12.7, "end": 14.9},
        ],
    },
    ...
]
```

Enrichment (fuzzy correction, VADER sentiment) lives ONE level up in
`transcribe.py` -- backends only do the STT.

Selection via `settings.transcribe_backend`. Adding a new backend =
new subclass of `TranscribeBackend` + one entry in `_BACKENDS`. Nothing
else in the pipeline changes.
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
from pathlib import Path
from typing import Protocol

from ..config import settings

_log = logging.getLogger(__name__)


class TranscribeUnavailableError(RuntimeError):
    """Raised when a backend can't be reached / credentials are missing.

    Callers convert this to an empty result -- transcription is always
    a best-effort candidate source, never fatal to the analyze job.
    """


class TranscribeBackend(Protocol):
    """Contract for a speech-to-text backend."""

    name: str

    def transcribe_raw(
        self,
        video_path: str,
        *,
        initial_prompt: str | None = None,
        condition_on_previous_text: bool = True,
    ) -> list[dict]:
        """Return raw segments -- (start_seconds, end_seconds, text,
        words). May raise `TranscribeUnavailableError`; any other
        exception should be caught + logged as a warning by callers so
        the wider analyze job survives.
        """
        ...


# ---------------------------------------------------------------------
# Local -- faster-whisper singleton (private, $0)
# ---------------------------------------------------------------------


_model = None


def _register_cuda_dlls() -> None:
    """Windows: nvidia pip wheels don't auto-add their DLLs to the
    search path, so ctranslate2 can't find cublas64_12.dll and cudnn64_9
    even after `pip install nvidia-cublas-cu12 nvidia-cudnn-cu12`. Add
    the bin dirs explicitly. No-op elsewhere."""
    import os
    import sys

    if os.name != "nt" or settings.whisper_device != "cuda":
        return
    venv_root = Path(sys.executable).parent.parent
    nvidia_root = venv_root / "Lib" / "site-packages" / "nvidia"
    for sub in ("cublas/bin", "cudnn/bin", "cuda_nvrtc/bin", "cuda_runtime/bin"):
        bin_dir = nvidia_root / sub
        if bin_dir.is_dir():
            os.add_dll_directory(str(bin_dir))


def _get_local_model():
    global _model
    if _model is None:
        _register_cuda_dlls()
        from faster_whisper import WhisperModel

        _model = WhisperModel(
            settings.whisper_model,
            device=settings.whisper_device,
            compute_type=settings.whisper_compute_type,
        )
    return _model


class LocalWhisperBackend:
    name = "local_whisper"

    def transcribe_raw(
        self,
        video_path: str,
        *,
        initial_prompt: str | None = None,
        condition_on_previous_text: bool = True,
    ) -> list[dict]:
        model = _get_local_model()
        segments, _info = model.transcribe(
            video_path,
            vad_filter=True,
            initial_prompt=initial_prompt,
            condition_on_previous_text=condition_on_previous_text,
            word_timestamps=True,
        )
        out: list[dict] = []
        for s in segments:
            text = (s.text or "").strip()
            if not text:
                continue
            words = [
                {
                    "word": (w.word or "").strip(),
                    "start": round(float(w.start), 3),
                    "end": round(float(w.end), 3),
                }
                for w in (s.words or [])
                if (w.word or "").strip()
            ]
            out.append(
                {
                    "start_seconds": round(float(s.start), 2),
                    "end_seconds": round(float(s.end), 2),
                    "text": text,
                    "words": words,
                }
            )
        return out


# ---------------------------------------------------------------------
# Groq -- cloud whisper-large-v3-turbo (OpenAI-compatible)
# ---------------------------------------------------------------------


def _extract_audio(video_path: str, out_wav: Path, stream_index: int | None = None) -> bool:
    """Extract mono 16k WAV from `video_path` via ffmpeg. Small
    (~24MB/hour) -- fits under Groq's file-size limit even for full
    VODs. `stream_index` picks the Nth audio stream (`-map 0:a:N`);
    None keeps ffmpeg's default. Returns True on success."""
    cmd = [
        settings.ffmpeg_path,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        video_path,
        "-vn",  # drop video stream
        *(["-map", f"0:a:{stream_index}"] if stream_index is not None else []),
        "-ac",
        "1",  # mono
        "-ar",
        "16000",  # whisper's native rate
        "-c:a",
        "pcm_s16le",
        str(out_wav),
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        _log.warning("groq: audio extract failed: %s", exc)
        return False
    if r.returncode != 0:
        _log.warning("groq: ffmpeg exit %d: %s", r.returncode, r.stderr[:500])
    return r.returncode == 0 and out_wav.is_file()


class GroqBackend:
    name = "groq"

    def transcribe_raw(
        self,
        video_path: str,
        *,
        initial_prompt: str | None = None,
        condition_on_previous_text: bool = True,
    ) -> list[dict]:
        # `condition_on_previous_text` is a faster-whisper knob that
        # doesn't map to Groq's API; noted here for parity.
        _ = condition_on_previous_text
        if not settings.groq_api_key:
            raise TranscribeUnavailableError(
                "GROQ_API_KEY is empty -- set it in .env to use the groq backend"
            )
        # Lazy import so httpx isn't required for local-only installs.
        import httpx

        with tempfile.TemporaryDirectory(prefix="groq_stt_") as tmp:
            audio_path = Path(tmp) / "audio.wav"
            if not _extract_audio(video_path, audio_path):
                raise TranscribeUnavailableError(
                    "ffmpeg failed to extract audio for Groq transcription"
                )

            # Groq's endpoint is OpenAI-compatible: multipart file upload,
            # `response_format=verbose_json` for segments, and
            # `timestamp_granularities[]=word` for word-level timing.
            url = f"{settings.groq_base_url}/audio/transcriptions"
            headers = {"Authorization": f"Bearer {settings.groq_api_key}"}
            data = {
                "model": settings.groq_transcribe_model,
                "response_format": "verbose_json",
                "timestamp_granularities[]": "word",
            }
            if initial_prompt:
                data["prompt"] = initial_prompt
            with audio_path.open("rb") as f:
                files = {"file": ("audio.wav", f, "audio/wav")}
                try:
                    resp = httpx.post(
                        url,
                        headers=headers,
                        data=data,
                        files=files,
                        timeout=600.0,
                    )
                except httpx.HTTPError as exc:
                    raise TranscribeUnavailableError(f"groq http error: {exc!s}") from exc
            if resp.status_code != 200:
                raise TranscribeUnavailableError(f"groq {resp.status_code}: {resp.text[:300]}")
            payload = resp.json()

        return _groq_payload_to_segments(payload)


def _groq_payload_to_segments(payload: dict) -> list[dict]:
    """Convert Groq's verbose_json response to our uniform segment shape.

    Groq returns:
      {
        "text": "...",
        "segments": [
            {"start": 1.2, "end": 3.4, "text": "...", ...},
            ...
        ],
        "words": [
            {"word": "hi", "start": 1.2, "end": 1.4},
            ...
        ]
      }

    Words are TOP-LEVEL (not per-segment), so we distribute them into
    segments by start-time overlap. If `segments` is missing we treat
    the whole payload as one segment.
    """
    segments_in = payload.get("segments") or []
    words_in = payload.get("words") or []
    if not segments_in:
        full_text = (payload.get("text") or "").strip()
        if not full_text:
            return []
        # Single-segment fallback -- rare, but keeps behavior sane if
        # Groq stops returning segment splits.
        end = 0.0
        for w in words_in:
            e = float(w.get("end") or 0.0)
            if e > end:
                end = e
        return [
            {
                "start_seconds": 0.0,
                "end_seconds": round(end, 2),
                "text": full_text,
                "words": [
                    {
                        "word": (w.get("word") or "").strip(),
                        "start": round(float(w.get("start") or 0.0), 3),
                        "end": round(float(w.get("end") or 0.0), 3),
                    }
                    for w in words_in
                    if (w.get("word") or "").strip()
                ],
            }
        ]

    # Distribute top-level words into segments by overlap.
    out: list[dict] = []
    word_idx = 0
    for seg in segments_in:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        s = round(float(seg.get("start") or 0.0), 2)
        e = round(float(seg.get("end") or 0.0), 2)
        seg_words = []
        while word_idx < len(words_in):
            w = words_in[word_idx]
            ws = float(w.get("start") or 0.0)
            we = float(w.get("end") or 0.0)
            if we <= s:
                # Word finished before this segment -- skip past.
                word_idx += 1
                continue
            if ws >= e:
                # Next segment territory.
                break
            token = (w.get("word") or "").strip()
            if token:
                seg_words.append(
                    {
                        "word": token,
                        "start": round(ws, 3),
                        "end": round(we, 3),
                    }
                )
            word_idx += 1
        out.append(
            {
                "start_seconds": s,
                "end_seconds": e,
                "text": text,
                "words": seg_words,
            }
        )
    return out


# ---------------------------------------------------------------------
# Registry + selector
# ---------------------------------------------------------------------


_BACKENDS: dict[str, type[TranscribeBackend]] = {
    "local_whisper": LocalWhisperBackend,
    "groq": GroqBackend,
}


def select_backend(name: str | None = None) -> TranscribeBackend:
    """Return the configured backend instance. Falls back to
    `local_whisper` if the requested backend is unknown (log a warning
    but never crash -- callers already treat this as best-effort)."""
    resolved = name or settings.transcribe_backend
    cls = _BACKENDS.get(resolved)
    if cls is None:
        _log.warning(
            "transcribe: unknown backend %r, falling back to local_whisper (known: %s)",
            resolved,
            sorted(_BACKENDS),
        )
        cls = LocalWhisperBackend
    return cls()
