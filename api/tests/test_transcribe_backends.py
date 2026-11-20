"""Tests for the pluggable transcribe backend layer.

Local backend needs faster-whisper installed and takes real audio, so
we cover it via existing integration tests. This suite exercises the
Groq payload parser + backend selector, which are pure enough to test
without hitting the network.
"""

from __future__ import annotations

from ai_video_editor.candidates.transcribe_backends import (
    GroqBackend,
    LocalWhisperBackend,
    _groq_payload_to_segments,
    select_backend,
)


def test_select_default_backend_is_local() -> None:
    backend = select_backend("local_whisper")
    assert isinstance(backend, LocalWhisperBackend)
    assert backend.name == "local_whisper"


def test_select_groq_backend() -> None:
    backend = select_backend("groq")
    assert isinstance(backend, GroqBackend)
    assert backend.name == "groq"


def test_select_unknown_falls_back_to_local(caplog) -> None:
    backend = select_backend("does-not-exist")
    assert isinstance(backend, LocalWhisperBackend)
    assert any("unknown backend" in r.message for r in caplog.records)


def test_groq_payload_parse_happy_path() -> None:
    payload = {
        "text": "hello there general kenobi",
        "segments": [
            {"start": 0.0, "end": 1.5, "text": "hello there"},
            {"start": 1.5, "end": 3.0, "text": "general kenobi"},
        ],
        "words": [
            {"word": "hello", "start": 0.0, "end": 0.7},
            {"word": "there", "start": 0.7, "end": 1.5},
            {"word": "general", "start": 1.5, "end": 2.3},
            {"word": "kenobi", "start": 2.3, "end": 3.0},
        ],
    }
    segments = _groq_payload_to_segments(payload)
    assert len(segments) == 2
    assert segments[0]["text"] == "hello there"
    assert segments[0]["start_seconds"] == 0.0
    assert segments[0]["end_seconds"] == 1.5
    assert [w["word"] for w in segments[0]["words"]] == ["hello", "there"]
    assert [w["word"] for w in segments[1]["words"]] == ["general", "kenobi"]


def test_groq_payload_empty_segments_falls_back_to_single() -> None:
    """When Groq returns text but no segments, use the whole text as
    one segment ending at the last word's end time."""
    payload = {
        "text": "clutch",
        "segments": [],
        "words": [{"word": "clutch", "start": 5.0, "end": 5.6}],
    }
    segments = _groq_payload_to_segments(payload)
    assert len(segments) == 1
    assert segments[0]["text"] == "clutch"
    assert segments[0]["end_seconds"] == 5.6


def test_groq_payload_empty_text_returns_empty() -> None:
    assert _groq_payload_to_segments({}) == []
    assert _groq_payload_to_segments({"segments": [], "words": []}) == []


def test_groq_payload_skips_empty_words_but_keeps_segment() -> None:
    payload = {
        "segments": [{"start": 0.0, "end": 1.0, "text": "hi"}],
        "words": [
            {"word": "", "start": 0.0, "end": 0.1},
            {"word": "hi", "start": 0.1, "end": 1.0},
        ],
    }
    segments = _groq_payload_to_segments(payload)
    assert len(segments) == 1
    assert [w["word"] for w in segments[0]["words"]] == ["hi"]


def test_groq_payload_word_before_segment_is_dropped() -> None:
    """Word occurring before the first segment start (edge case) should
    not attach to segment 0."""
    payload = {
        "segments": [{"start": 2.0, "end": 3.0, "text": "later"}],
        "words": [
            {"word": "earlier", "start": 0.0, "end": 1.5},
            {"word": "later", "start": 2.1, "end": 3.0},
        ],
    }
    segments = _groq_payload_to_segments(payload)
    assert [w["word"] for w in segments[0]["words"]] == ["later"]
