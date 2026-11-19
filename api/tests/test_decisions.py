"""Typed decision adapter: rules / Haiku / Jev backends, selection,
fallback, spend guard, tracing and the pre-filter. No network."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from ai_video_editor import anthropic_surface, decisions
from ai_video_editor.bucketing import prefilter_candidates, route_clip
from ai_video_editor.config import settings
from ai_video_editor.decisions import (
    Answer,
    BoolQuestion,
    ChoiceQuestion,
    ScoreQuestion,
    ask,
    select_backend,
)
from ai_video_editor.decisions.backends import BACKENDS, haiku, jev
from ai_video_editor.taxonomy import Format, Topic


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    monkeypatch.setattr(anthropic_surface, "_calls", {})
    monkeypatch.setattr(settings, "typesafe_api_key", None)
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "decisions_backend", "auto")


FORMAT_Q = ChoiceQuestion(prompt="Which format?", options=Format)


class FakeClient:
    """Stands in for anthropic.Anthropic; `replies` are consumed per call."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls: list[dict] = []
        self.messages = SimpleNamespace(parse=self._parse)

    def _parse(self, **kwargs):
        self.calls.append(kwargs)
        reply = self.replies.pop(0)
        if reply is None:
            return _response(None)
        return _response(kwargs["output_format"](**reply))


def _response(parsed):
    return SimpleNamespace(
        parsed_output=parsed,
        model="claude-haiku-4-5",
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
    )


def _use_client(monkeypatch, client):
    monkeypatch.setattr(haiku, "make_client", lambda: client)


# --- models + rules ---


def test_choice_question_requires_enum_options():
    with pytest.raises(ValidationError):
        ChoiceQuestion(prompt="x", options=str)


@pytest.mark.parametrize(
    "clip",
    [
        {"event": "ace"},
        {"event": "pentakill"},
        {"event": "funny_callout"},
        {"event": "kill", "funny_score": 0.9, "hype_score": 0.1},
        {"event": "kill"},
        {"event": "audio_peak"},
        {"event_type": "multi_kill_2"},
        {"event": "never_heard_of_it"},
    ],
)
def test_rules_backend_reproduces_bucketing_routing(monkeypatch, clip):
    monkeypatch.setattr(settings, "decisions_backend", "rules")
    answer = ask(FORMAT_Q, {"clip": clip})
    assert answer.backend == "rules"
    assert answer.value == route_clip(clip)[0]
    assert answer.probability == 1.0


def test_rules_topic_maps_multikill_events():
    answer = ask(ChoiceQuestion(prompt="t", options=Topic), {"clip": {"event": "ace"}})
    assert answer.value == Topic.multikill


def test_rules_has_no_opinion_on_unknown_questions():
    answer = ask(ScoreQuestion(prompt="s", lo=0, hi=10), {})
    assert answer.backend == "rules"
    assert answer.value is None
    assert answer.probability == 0.0


def test_rules_bool_includes_by_default():
    answer = ask(BoolQuestion(prompt="include?"), {"candidate": {}})
    assert answer.value is True


# --- haiku ---


def test_haiku_choice_schema_is_literal_of_enum_values(monkeypatch):
    client = FakeClient([{"value": "hype", "probability": 0.8}])
    _use_client(monkeypatch, client)
    answer = haiku.answer(FORMAT_Q, {"clip": {"event": "ace"}})
    assert answer == Answer(value=Format.hype, probability=0.8, backend="haiku")
    call = client.calls[0]
    assert call["model"] == settings.ranker_model
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    schema = call["output_format"].model_json_schema()
    assert set(schema["properties"]["value"]["enum"]) == {f.value for f in Format}


def test_haiku_bool_and_score(monkeypatch):
    client = FakeClient(
        [
            {"value": False, "probability": 0.9},
            {"value": 7, "probability": 0.6},
        ]
    )
    _use_client(monkeypatch, client)
    assert haiku.answer(BoolQuestion(prompt="b"), {}).value is False
    score = haiku.answer(ScoreQuestion(prompt="s", lo=1, hi=10), {})
    assert score.value == 7
    bounded = client.calls[1]["output_format"]
    with pytest.raises(ValidationError):
        bounded(value=11, probability=0.5)


def test_haiku_retries_once_on_malformed_then_succeeds(monkeypatch):
    client = FakeClient([None, {"value": "funny", "probability": 0.7}])
    _use_client(monkeypatch, client)
    answer = haiku.answer(FORMAT_Q, {})
    assert answer.value == Format.funny
    assert len(client.calls) == 2


def test_haiku_malformed_twice_falls_back_to_rules(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-test")
    client = FakeClient([None, None])
    _use_client(monkeypatch, client)
    answer = ask(FORMAT_Q, {"clip": {"event": "ace"}})
    assert answer.backend == "rules"
    assert answer.value == Format.hype
    assert len(client.calls) == 2


def test_every_haiku_call_is_counted_by_the_spend_guard(monkeypatch):
    client = FakeClient([None, {"value": "hype", "probability": 0.5}])
    _use_client(monkeypatch, client)
    haiku.answer(FORMAT_Q, {})
    assert anthropic_surface._calls["decisions"] == 2


def test_spend_cap_falls_back_to_rules_without_calling_anthropic(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-test")
    monkeypatch.setattr(settings, "decisions_max_calls", 0)
    client = FakeClient([])
    _use_client(monkeypatch, client)
    answer = ask(FORMAT_Q, {"clip": {"event": "ace"}})
    assert answer.backend == "rules"
    assert client.calls == []


def test_haiku_call_is_traced_with_backend_metadata(monkeypatch):
    seen = {}

    class FakeLangfuse:
        def update_current_generation(self, **kw):
            seen["generation"] = kw

        def update_current_trace(self, **kw):
            seen["trace"] = kw

    monkeypatch.setattr(anthropic_surface, "get_client", lambda: FakeLangfuse())
    _use_client(monkeypatch, FakeClient([{"value": "hype", "probability": 0.8}]))
    haiku.answer(FORMAT_Q, {})
    assert seen["generation"]["model"] == "claude-haiku-4-5"
    assert seen["generation"]["usage_details"] == {"input": 10, "output": 5}
    assert seen["generation"]["metadata"] == {"backend": "haiku"}
    assert seen["trace"]["metadata"] == {"backend": "haiku"}


# --- jev ---


class FakeTransport:
    def __init__(self):
        self.requests: list[tuple] = []

    def choice(self, prompt, options, state):
        self.requests.append(("choice", prompt, options))
        return options[1], 0.9

    def score(self, prompt, lo, hi, state):
        self.requests.append(("score", prompt, lo, hi))
        return hi, 0.8

    def bool(self, prompt, state):
        self.requests.append(("bool", prompt))
        return True, 0.7


def test_jev_maps_each_question_kind_to_the_transport():
    transport = FakeTransport()
    choice = jev.answer(FORMAT_Q, {}, transport=transport)
    assert choice == Answer(value=Format.story, probability=0.9, backend="jev")
    assert transport.requests[0][2] == [f.value for f in Format]
    score = jev.answer(ScoreQuestion(prompt="s", lo=0, hi=5), {}, transport=transport)
    assert score.value == 5
    assert jev.answer(BoolQuestion(prompt="b"), {}, transport=transport).value is True


def test_jev_without_a_transport_is_an_error_not_a_guess():
    with pytest.raises(decisions.DecisionError):
        jev.answer(BoolQuestion(prompt="b"), {})


# --- selection ---


def test_registry_has_one_entry_per_backend():
    assert set(BACKENDS) == {"jev", "haiku", "rules"}


def test_select_defaults_to_rules_with_no_keys():
    assert select_backend() == "rules"


def test_select_auto_prefers_jev_then_haiku(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "sk")
    assert select_backend() == "haiku"
    monkeypatch.setattr(settings, "typesafe_api_key", "ts")
    assert select_backend() == "jev"


def test_select_explicit_without_credentials_falls_back_to_rules(monkeypatch):
    monkeypatch.setattr(settings, "decisions_backend", "jev")
    assert select_backend() == "rules"
    monkeypatch.setattr(settings, "decisions_backend", "haiku")
    assert select_backend() == "rules"


def test_select_explicit_haiku_ignores_jev_key(monkeypatch):
    monkeypatch.setattr(settings, "decisions_backend", "haiku")
    monkeypatch.setattr(settings, "anthropic_api_key", "sk")
    monkeypatch.setattr(settings, "typesafe_api_key", "ts")
    assert select_backend() == "haiku"


def test_ask_uses_the_selected_backend(monkeypatch):
    monkeypatch.setitem(
        BACKENDS, "rules", lambda q, s: Answer(value=1, probability=1.0, backend="rules")
    )
    assert ask(ScoreQuestion(prompt="s", lo=0, hi=1), {}).value == 1


# --- prefilter ---


def _fake_ask(probabilities):
    def _ask(question, state):
        p = probabilities[state["candidate"]["id"]]
        return Answer(value=p >= 0.5, probability=p if p >= 0.5 else 1 - p, backend="rules")

    return _ask


def test_prefilter_splits_on_include_probability(monkeypatch):
    monkeypatch.setattr(settings, "prefilter_min_probability", 0.35)
    cands = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
    kept, dropped = prefilter_candidates(cands, ask=_fake_ask({"a": 0.9, "b": 0.2, "c": 0.4}))
    assert [c["id"] for c in kept] == ["a", "c"]
    assert dropped == [{"id": "b", "prefilter_probability": 0.2}]
    assert "prefilter_probability" not in kept[0]


def test_prefilter_with_rules_keeps_everything(monkeypatch):
    monkeypatch.setattr(settings, "decisions_backend", "rules")
    cands = [{"id": "a", "event_type": "audio_peak"}, {"id": "b"}]
    kept, dropped = prefilter_candidates(cands)
    assert kept == cands
    assert dropped == []


def test_compute_candidates_prefilters_and_reports_dropped(monkeypatch):
    from ai_video_editor.candidates import service

    monkeypatch.setattr(service, "get_duration_seconds", lambda _p: 26.0)
    monkeypatch.setattr(
        service,
        "prefilter_candidates",
        lambda rows: ([], [{**r, "prefilter_probability": 0.1} for r in rows]),
    )
    rows, diag = service.compute_candidates({"id": "v1", "path": "x.mp4", "game": None})
    assert rows == []
    assert diag["prefilter_dropped"][0]["prefilter_probability"] == 0.1
    assert diag["prefilter_dropped"][0]["source"] == "outplayed_clip"
