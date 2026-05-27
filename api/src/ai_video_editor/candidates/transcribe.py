"""Local speech-to-text (faster-whisper).

Private by design: runs entirely on this machine, never uploads audio —
consistent with the project's local-first principle. The model is loaded
once (lazy singleton) and reused. Fully non-fatal: any failure (missing
model, decode error, over-long input) returns [] so candidate
generation still works without transcripts.

Output is a list of timestamped segments — a reusable corpus that feeds
the transcript_keyword source now and RAG/semantic search later.
"""

import logging

from ..config import settings

_log = logging.getLogger(__name__)
_model = None  # lazy WhisperModel singleton (load is expensive)


def _register_cuda_dlls() -> None:
    """Windows: nvidia pip wheels don't auto-add their DLLs to the search
    path, so ctranslate2 can't find cublas64_12.dll + cudnn64_9.dll even
    after `pip install nvidia-cublas-cu12 nvidia-cudnn-cu12`. Add the
    bin dirs explicitly. No-op on non-Windows or when device != cuda.
    """
    import os
    import sys
    from pathlib import Path

    if os.name != "nt" or settings.whisper_device != "cuda":
        return
    venv_root = Path(sys.executable).parent.parent
    nvidia_root = venv_root / "Lib" / "site-packages" / "nvidia"
    for sub in ("cublas/bin", "cudnn/bin", "cuda_nvrtc/bin", "cuda_runtime/bin"):
        bin_dir = nvidia_root / sub
        if bin_dir.is_dir():
            os.add_dll_directory(str(bin_dir))


def _get_model():
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


def transcribe(video_path: str, duration: float, game: str | None = None) -> list[dict]:
    """Return [{start_seconds, end_seconds, text}, ...] or [] on any issue.

    Guarded: refuses pathologically long inputs (CPU STT is ~real-time,
    so a multi-hour VOD would block the job for hours).

    When `game` is set, looks up the profile's `[transcription]` section
    and biases the model toward its `initial_prompt`. We also disable
    `condition_on_previous_text` so the bias survives past the first
    30s (Whisper would otherwise overwrite the prompt with the running
    decoder context — a documented quirk that silently degrades vocab
    accuracy on long recordings). Game omitted / unknown → stock Whisper.
    """
    if duration <= 0 or duration > settings.transcribe_max_seconds:
        _log.warning(
            "transcribe skipped: duration %.0fs vs cap %.0fs",
            duration,
            settings.transcribe_max_seconds,
        )
        return []

    initial_prompt: str | None = None
    condition_on_previous_text = True  # faster-whisper default
    vocabulary: list[str] = []
    name_aliases: dict[str, list[str]] = {}
    if game:
        from ..profiles import load_profile  # local import — keeps STT importable in tests

        profile = load_profile(game)
        if profile.transcription.initial_prompt:
            initial_prompt = profile.transcription.initial_prompt
            # When biasing, force per-chunk re-priming so the prompt
            # actually applies past 30s.
            condition_on_previous_text = False
        vocabulary = profile.transcription.vocabulary
        name_aliases = profile.transcription.name_aliases

    try:
        model = _get_model()
        # vad_filter drops silence — big speedup on gameplay (long quiet
        # stretches) and tighter segments.
        segments, _info = model.transcribe(
            video_path,
            vad_filter=True,
            initial_prompt=initial_prompt,
            condition_on_previous_text=condition_on_previous_text,
            # Powers TikTok-style word-by-word captioning. Cheap
            # add — the model already produces token-level alignments
            # internally; this just surfaces them.
            word_timestamps=True,
        )
        # Post-process: fuzzy-correct vocab misses (second-line cleanup
        # after Whisper's prompt bias) and stamp arousal-based sentiment
        # so the ranker / candidate generation can prefer excited speech.
        from .fuzzy_correct import fuzzy_correct
        from .sentiment import score_sentiment

        out: list[dict] = []
        for s in segments:
            text = (s.text or "").strip()
            if not text:
                continue
            if vocabulary or name_aliases:
                text = fuzzy_correct(text, vocabulary, aliases=name_aliases)
            # Per-word timing for TikTok-style captions. Whisper returns
            # `s.words` when word_timestamps=True; we serialize to the
            # minimum shape downstream needs.
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
                    "sentiment_score": score_sentiment(text),
                    "words": words,
                }
            )
        return out
    except Exception as e:  # never fatal — other sources still work
        _log.warning("transcribe failed (non-fatal): %s", e)
        return []
