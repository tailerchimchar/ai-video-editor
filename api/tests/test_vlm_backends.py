"""Backend selection + shared retry logic tests.

No live Ollama — a stub VLMBackend implementation stands in for HTTP.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_video_editor.vlm.backends.base import (
    VLMUnavailableError,
    _strip_code_fence,
    run_with_retry,
)
from ai_video_editor.vlm.client import UnsupportedVLMBackendError, select_backend
from ai_video_editor.vlm.prompts import ClipVerdict


class _StubBackend:
    """Yields a canned sequence of raw text responses, one per call."""

    name = "stub"

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[str] = []

    def health(self) -> dict:
        return {"ok": True, "model": "stub", "latency_ms": 0}

    def call(self, *, frames, system_prompt: str, user_prompt: str) -> str:
        self.calls.append(user_prompt)
        return self._responses.pop(0)


class _UnavailableBackend:
    name = "unavailable"

    def health(self) -> dict:
        return {"ok": False, "reason": "stub unavailable"}

    def call(self, **_kw) -> str:
        raise VLMUnavailableError("stub unavailable")


# ---------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------


def test_select_backend_ollama_default() -> None:
    backend = select_backend("ollama")
    assert backend.name == "ollama"


def test_select_backend_case_insensitive() -> None:
    backend = select_backend("OLLAMA")
    assert backend.name == "ollama"


def test_select_backend_unknown_raises() -> None:
    with pytest.raises(UnsupportedVLMBackendError):
        select_backend("gemini-vision")


# ---------------------------------------------------------------------
# _strip_code_fence
# ---------------------------------------------------------------------


def test_strip_code_fence_bare_json_unchanged() -> None:
    s = '{"verdict": "pass"}'
    assert _strip_code_fence(s) == s


def test_strip_code_fence_removes_language_tag() -> None:
    s = '```json\n{"verdict": "pass"}\n```'
    assert _strip_code_fence(s) == '{"verdict": "pass"}'


def test_strip_code_fence_removes_bare_fence() -> None:
    s = '```\n{"verdict": "pass"}\n```'
    assert _strip_code_fence(s) == '{"verdict": "pass"}'


# ---------------------------------------------------------------------
# run_with_retry
# ---------------------------------------------------------------------


def test_run_with_retry_first_response_valid() -> None:
    backend = _StubBackend(
        [
            '{"verdict": "pass", "why": "ok", "fix": null, "fix_seconds": null}'
        ]
    )
    verdict = run_with_retry(
        backend,
        frames=[],
        system_prompt="sys",
        user_prompt="user",
        response_model=ClipVerdict,
        max_retries=2,
    )
    assert verdict.verdict == "pass"
    assert len(backend.calls) == 1


def test_run_with_retry_recovers_after_bad_json() -> None:
    backend = _StubBackend(
        [
            "here's my verdict: pass",  # unparseable
            '{"verdict": "pass", "why": "ok"}',
        ]
    )
    verdict = run_with_retry(
        backend,
        frames=[],
        system_prompt="sys",
        user_prompt="user",
        response_model=ClipVerdict,
        max_retries=2,
    )
    assert verdict.verdict == "pass"
    # Second call must have gotten the retry hint appended
    assert "ONLY valid JSON" in backend.calls[1]


def test_run_with_retry_exhausts_retries() -> None:
    from pydantic import ValidationError

    backend = _StubBackend(["garbage 1", "garbage 2", "garbage 3"])
    with pytest.raises((ValueError, ValidationError)):
        run_with_retry(
            backend,
            frames=[],
            system_prompt="sys",
            user_prompt="user",
            response_model=ClipVerdict,
            max_retries=2,
        )
    assert len(backend.calls) == 3  # initial + 2 retries


def test_run_with_retry_recovers_after_schema_violation() -> None:
    backend = _StubBackend(
        [
            '{"verdict": "kinda_ok", "why": "x"}',  # invalid enum
            '{"verdict": "pass", "why": "x"}',
        ]
    )
    verdict = run_with_retry(
        backend,
        frames=[],
        system_prompt="sys",
        user_prompt="user",
        response_model=ClipVerdict,
        max_retries=2,
    )
    assert verdict.verdict == "pass"


def test_run_with_retry_does_not_retry_on_unavailable() -> None:
    backend = _UnavailableBackend()
    with pytest.raises(VLMUnavailableError):
        run_with_retry(
            backend,
            frames=[],
            system_prompt="sys",
            user_prompt="user",
            response_model=ClipVerdict,
            max_retries=5,
        )


def test_run_with_retry_frames_and_prompts_forwarded() -> None:
    frames = [Path("/tmp/f1.jpg"), Path("/tmp/f2.jpg")]

    class _Capture(_StubBackend):
        def __init__(self) -> None:
            super().__init__(['{"verdict": "pass", "why": "ok"}'])
            self.frames_seen: list[Path] = []
            self.system_seen: str = ""

        def call(self, *, frames, system_prompt: str, user_prompt: str) -> str:
            self.frames_seen = list(frames)
            self.system_seen = system_prompt
            return super().call(
                frames=frames, system_prompt=system_prompt, user_prompt=user_prompt
            )

    cap = _Capture()
    run_with_retry(
        cap,
        frames=frames,
        system_prompt="THE-SYS",
        user_prompt="THE-USER",
        response_model=ClipVerdict,
        max_retries=1,
    )
    assert cap.frames_seen == frames
    assert cap.system_seen == "THE-SYS"
