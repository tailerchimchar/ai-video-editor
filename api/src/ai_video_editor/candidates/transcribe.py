"""Speech-to-text -> segments pipeline. Backend-agnostic.

Two-layer design:

- **Backend layer** (`transcribe_backends.py`) -- raw STT. Currently
  `local_whisper` (private, on-device via faster-whisper) and `groq`
  (cloud, ~$0.02/VOD via whisper-large-v3-turbo). Selected by
  `settings.transcribe_backend`.
- **Enrichment layer** (this module) -- game-profile prompt bias,
  fuzzy-correct vocab misses, VADER sentiment scoring. Applied to the
  segments returned by whichever backend is active.

Non-fatal: any backend failure (missing model, network error, etc.)
returns `[]` so candidate generation still works without transcripts.
"""

import logging

from ..config import settings
from .transcribe_backends import TranscribeUnavailableError, select_backend

_log = logging.getLogger(__name__)


def transcribe(video_path: str, duration: float, game: str | None = None) -> list[dict]:
    """Return [{start_seconds, end_seconds, text, sentiment_score,
    words}, ...] or [] on any issue.

    Guarded: refuses pathologically long inputs (CPU STT is ~real-time,
    so a multi-hour VOD would block the job for hours). The guard
    applies to ALL backends -- Groq is fast but 90+ minutes at once is
    still a poor UX.

    When `game` is set, looks up the profile's `[transcription]` section
    and biases the model toward its `initial_prompt`. Also disables
    `condition_on_previous_text` for the local backend so the bias
    survives past the first 30s (Whisper would otherwise overwrite the
    prompt with the running decoder context -- a documented quirk).
    Game omitted / unknown -> stock backend defaults.
    """
    if duration <= 0 or duration > settings.transcribe_max_seconds:
        _log.warning(
            "transcribe skipped: duration %.0fs vs cap %.0fs",
            duration,
            settings.transcribe_max_seconds,
        )
        return []

    initial_prompt: str | None = None
    condition_on_previous_text = True
    vocabulary: list[str] = []
    name_aliases: dict[str, list[str]] = {}
    if game:
        from ..profiles import load_profile  # local import — keeps STT importable in tests

        profile = load_profile(game)
        if profile.transcription.initial_prompt:
            initial_prompt = profile.transcription.initial_prompt
            condition_on_previous_text = False
        vocabulary = profile.transcription.vocabulary
        name_aliases = profile.transcription.name_aliases

    backend = select_backend()
    try:
        raw_segments = backend.transcribe_raw(
            video_path,
            initial_prompt=initial_prompt,
            condition_on_previous_text=condition_on_previous_text,
        )
    except TranscribeUnavailableError as exc:
        _log.warning("transcribe: backend %s unavailable: %s", backend.name, exc)
        return []
    except Exception as exc:  # never fatal — other sources still work
        _log.warning("transcribe: backend %s failed (non-fatal): %s", backend.name, exc)
        return []

    if not raw_segments:
        return []

    # Enrichment: fuzzy vocab correction + VADER sentiment scoring.
    from .fuzzy_correct import fuzzy_correct
    from .sentiment import score_sentiment

    out: list[dict] = []
    for seg in raw_segments:
        text = seg.get("text", "").strip()
        if not text:
            continue
        if vocabulary or name_aliases:
            text = fuzzy_correct(text, vocabulary, aliases=name_aliases)
        out.append(
            {
                "start_seconds": seg.get("start_seconds"),
                "end_seconds": seg.get("end_seconds"),
                "text": text,
                "sentiment_score": score_sentiment(text),
                "words": seg.get("words") or [],
            }
        )
    return out
