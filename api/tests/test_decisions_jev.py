"""Jev backend: typed question/answer mapping onto TypeSafe's System One
API, one request per state, the HTTP transport and the call cap. No network."""

from __future__ import annotations

import json

import httpx
import pytest

from ai_video_editor import anthropic_surface
from ai_video_editor.config import settings
from ai_video_editor.decisions import (
    Answer,
    BoolQuestion,
    ChoiceQuestion,
    DecisionError,
    ScoreQuestion,
    select_backend,
)
from ai_video_editor.decisions.backends import BACKENDS, jev
from ai_video_editor.taxonomy import Format


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    monkeypatch.setattr(anthropic_surface, "_calls", {})
    monkeypatch.setattr(settings, "typesafe_api_key", "ts-key")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "decisions_backend", "auto")


FORMAT_Q = ChoiceQuestion(
    prompt="Which format?", options=Format, descriptions={"hype": "big mechanical plays"}
)
KEEP_Q = BoolQuestion(prompt="Keep it?")
SCORE_Q = ScoreQuestion(prompt="How intense?", lo=1, hi=5)


class RecordingTransport:
    """Answers every question with `reply(key, question)`; records calls."""

    def __init__(self, reply):
        self.reply = reply
        self.calls: list[tuple[dict, dict]] = []

    def ask(self, state, questions):
        self.calls.append((state, questions))
        return {key: self.reply(key, question) for key, question in questions.items()}


def _canned(key, question):
    if question["type"] == "choice":
        return {"type": "choice", "choice": "funny", "probabilities": {"funny": 0.8}}
    if question["type"] == "noul":
        return {"type": "noul", "noul": 0.9}
    return {"type": "score", "score": 1.4, "confidence": 0.4, "probabilities": {"1": 0.6}}


# --- question mapping ---


def test_choice_maps_to_jev_choice_with_descriptions_defaulting_to_the_name():
    assert jev.to_jev_question(FORMAT_Q) == {
        "type": "choice",
        "instructions": "Which format?",
        "criteria": {
            "hype": "big mechanical plays",
            "story": "story",
            "voice_over": "voice_over",
            "funny": "funny",
            "arc_montage": "arc_montage",
        },
    }


def test_bool_maps_to_noul_and_score_to_an_ordered_rubric():
    assert jev.to_jev_question(KEEP_Q) == {"type": "noul", "instructions": "Keep it?"}
    assert jev.to_jev_question(SCORE_Q) == {
        "type": "score",
        "instructions": "How intense?",
        "criteria": ["1", "2", "3", "4", "5"],
    }


def test_format_question_describes_every_format_for_jev():
    from ai_video_editor.bucketing import _FORMAT_QUESTION

    assert set(_FORMAT_QUESTION.descriptions) == {f.value for f in Format}


# --- answer mapping ---


def test_choice_answer_uses_the_calibrated_probability_of_the_pick():
    raw = {
        "type": "choice",
        "choice": "story",
        "confidence": 0.6,
        "probabilities": {"story": 0.7, "hype": 0.3},
    }
    assert jev.from_jev_answer(FORMAT_Q, raw) == Answer(
        value=Format.story, probability=0.7, backend="jev"
    )


def test_noul_answer_probability_is_confidence_in_its_own_value():
    assert jev.from_jev_answer(KEEP_Q, {"type": "noul", "noul": 0.2}) == Answer(
        value=False, probability=0.8, backend="jev"
    )
    assert jev.from_jev_answer(KEEP_Q, {"type": "noul", "noul": 0.75}).value is True


def test_score_answer_rounds_the_expected_level_back_into_lo_hi():
    raw = {
        "type": "score",
        "score": 2.6,
        "confidence": 0.5,
        "probabilities": {"0": 0.1, "1": 0.1, "2": 0.2, "3": 0.6, "4": 0.0},
    }
    assert jev.from_jev_answer(SCORE_Q, raw) == Answer(value=4, probability=0.6, backend="jev")


def test_mismatched_or_unknown_answers_are_decision_errors():
    with pytest.raises(DecisionError):
        jev.from_jev_answer(FORMAT_Q, {"type": "noul", "noul": 0.5})
    with pytest.raises(DecisionError):
        jev.from_jev_answer(FORMAT_Q, {"type": "choice", "choice": "nope", "probabilities": {}})


# --- batching: one request per state, every question for it in that request ---


def test_answer_many_sends_one_request_per_state_and_keeps_item_order():
    transport = RecordingTransport(_canned)
    s1, s2 = {"clip": {"id": 1}}, {"clip": {"id": 2}}
    answers = jev.answer_many([(FORMAT_Q, s1), (KEEP_Q, s1), (SCORE_Q, s2)], transport=transport)

    assert [call[0] for call in transport.calls] == [s1, s2]
    assert [q["type"] for q in transport.calls[0][1].values()] == ["choice", "noul"]
    assert [a.value for a in answers] == [Format.funny, True, 2]  # score level 1 -> lo + 1
    assert [a.backend for a in answers] == ["jev"] * 3


def test_transport_failure_is_wrapped_so_ask_many_can_fall_back():
    class Down:
        def ask(self, state, questions):
            raise ConnectionError("down")

    with pytest.raises(DecisionError, match="jev call failed"):
        jev.answer(KEEP_Q, {}, transport=Down())


def test_call_cap_refuses_before_the_transport_is_touched(monkeypatch):
    monkeypatch.setattr(settings, "typesafe_max_calls", 0)
    transport = RecordingTransport(_canned)
    with pytest.raises(DecisionError, match="cap"):
        jev.answer(KEEP_Q, {}, transport=transport)
    assert transport.calls == []


# --- selection + registry ---


def test_key_alone_makes_jev_available_and_auto_selects_it(monkeypatch):
    assert jev.available() is True
    assert select_backend() == "jev"
    assert BACKENDS["jev"].answer_many is jev.answer_many
    monkeypatch.setattr(settings, "typesafe_api_key", None)
    assert jev.available() is False
    assert select_backend() == "rules"


# --- HTTP transport ---


def test_http_transport_posts_a_system_one_request_with_bearer_auth():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": "jev-1.13.0",
                "answers": {"keep": {"type": "noul", "noul": 0.9}},
                "usage": {"input_tokens": 12, "output_tokens": 0},
            },
        )

    transport = jev.HttpTransport(
        "ts-key", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    answers = transport.ask({"clip": {"id": 1}}, {"keep": jev.to_jev_question(KEEP_Q)})

    assert answers == {"keep": {"type": "noul", "noul": 0.9}}
    assert seen["url"] == "https://api.typesafe.ai/v1/systemone"
    assert seen["auth"] == "Bearer ts-key"
    assert seen["body"] == {
        "model": "jev-latest",
        "state": {"clip": {"id": 1}},
        "questions": {"keep": {"type": "noul", "instructions": "Keep it?"}},
    }


def test_http_transport_surfaces_api_errors_with_their_detail():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": [{"msg": "criteria required"}]})

    transport = jev.HttpTransport(
        "ts-key", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(DecisionError, match=r"422.*criteria required"):
        jev.answer(KEEP_Q, {}, transport=transport)


def test_default_transport_is_the_http_one_when_a_key_is_set(monkeypatch):
    assert isinstance(jev.default_transport(), jev.HttpTransport)
    monkeypatch.setattr(settings, "typesafe_api_key", None)
    assert jev.default_transport() is None
