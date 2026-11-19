"""Typed decision adapter: rules / Haiku / Jev backends, selection,
batching, fallback, spend guard, tracing and the pre-filter. No network."""

from __future__ import annotations

import json
import math
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from ai_video_editor import anthropic_surface, decisions
from ai_video_editor.bucketing import classify_formats, prefilter_candidates, route_clip
from ai_video_editor.config import settings
from ai_video_editor.decisions import (
    Answer,
    BoolQuestion,
    ChoiceQuestion,
    ScoreQuestion,
    ask,
    select_backend,
)
from ai_video_editor.decisions.backends import BACKENDS, Backend, haiku, jev
from ai_video_editor.taxonomy import Format, Topic


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    monkeypatch.setattr(anthropic_surface, "_calls", {})
    monkeypatch.setattr(settings, "typesafe_api_key", None)
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "decisions_backend", "auto")


FORMAT_Q = ChoiceQuestion(prompt="Which format?", options=Format)


class FakeClient:
    """Stands in for anthropic.Anthropic. Each reply is None (malformed) or
    a list of `(value, probability)`, one per item in that call."""

    def __init__(self, replies=None):
        self.replies = list(replies or [])
        self.calls: list[dict] = []
        self.messages = SimpleNamespace(parse=self._parse)

    def _parse(self, **kwargs):
        self.calls.append(kwargs)
        reply = self.replies.pop(0) if self.replies else None
        if reply is None:
            return _response(None)
        results = [{"index": i, "value": v, "probability": p} for i, (v, p) in enumerate(reply)]
        return _response(kwargs["output_format"](results=results))


def _response(parsed):
    return SimpleNamespace(
        parsed_output=parsed,
        model="claude-haiku-4-5",
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
    )


def _use_client(monkeypatch, client):
    monkeypatch.setattr(haiku, "make_client", lambda: client)


def _explicit_haiku(monkeypatch):
    monkeypatch.setattr(settings, "decisions_backend", "haiku")
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-test")


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


@pytest.mark.parametrize("event", ["doublekill", "triplekill", "multi_kill_5", "pentakill"])
def test_rules_topic_maps_every_multikill_alias(event):
    answer = ask(ChoiceQuestion(prompt="t", options=Topic), {"clip": {"event": event}})
    assert answer.value == Topic.multikill


def test_rules_topic_has_no_opinion_on_a_single_kill():
    answer = ask(ChoiceQuestion(prompt="t", options=Topic), {"clip": {"event": "kill"}})
    assert answer.value is None


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
    client = FakeClient([[("hype", 0.8)]])
    _use_client(monkeypatch, client)
    answer = haiku.answer(FORMAT_Q, {"clip": {"event": "ace"}})
    assert answer == Answer(value=Format.hype, probability=0.8, backend="haiku")
    call = client.calls[0]
    assert call["model"] == settings.ranker_model
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    item = call["output_format"].model_json_schema()["$defs"]["DecisionItem"]
    assert set(item["properties"]["value"]["enum"]) == {f.value for f in Format}


def test_haiku_bool_and_score(monkeypatch):
    client = FakeClient([[(False, 0.9)], [(7, 0.6)]])
    _use_client(monkeypatch, client)
    assert haiku.answer(BoolQuestion(prompt="b"), {}).value is False
    assert haiku.answer(ScoreQuestion(prompt="s", lo=1, hi=10), {}).value == 7
    batch = client.calls[1]["output_format"]
    with pytest.raises(ValidationError):
        batch(results=[{"index": 0, "value": 11, "probability": 0.5}])


def test_haiku_retries_once_on_malformed_then_succeeds(monkeypatch):
    client = FakeClient([None, [("funny", 0.7)]])
    _use_client(monkeypatch, client)
    answer = haiku.answer(FORMAT_Q, {})
    assert answer.value == Format.funny
    assert len(client.calls) == 2


def test_haiku_reply_with_wrong_item_count_is_malformed(monkeypatch):
    client = FakeClient([[("hype", 0.5)], [("hype", 0.5), ("funny", 0.5)]])
    _use_client(monkeypatch, client)
    answers = haiku.answer_many([(FORMAT_Q, {}), (FORMAT_Q, {})])
    assert [a.value for a in answers] == [Format.hype, Format.funny]
    assert len(client.calls) == 2


def test_haiku_malformed_twice_falls_back_to_rules(monkeypatch):
    _explicit_haiku(monkeypatch)
    client = FakeClient([None, None])
    _use_client(monkeypatch, client)
    answer = ask(FORMAT_Q, {"clip": {"event": "ace"}})
    assert answer.backend == "rules"
    assert answer.value == Format.hype
    assert len(client.calls) == 2


def test_100_candidates_cost_ceil_100_over_batch_size_calls(monkeypatch):
    _explicit_haiku(monkeypatch)
    monkeypatch.setattr(settings, "decisions_batch_size", 40)
    client = FakeClient([[(True, 0.9)] * 40, [(True, 0.9)] * 40, [(True, 0.9)] * 20])
    _use_client(monkeypatch, client)
    cands = [{"id": str(i)} for i in range(100)]
    kept, dropped = prefilter_candidates(cands)
    assert len(client.calls) == math.ceil(100 / 40) == 3
    assert anthropic_surface._calls["decisions"] == 3
    assert len(kept) == 100 and dropped == []


def test_haiku_batch_keeps_item_order_and_state(monkeypatch):
    client = FakeClient([[(False, 0.9), (True, 0.8)]])
    _use_client(monkeypatch, client)
    answers = haiku.answer_many(
        [(BoolQuestion(prompt="b"), {"n": "first"}), (BoolQuestion(prompt="b"), {"n": "second"})]
    )
    assert [a.value for a in answers] == [False, True]
    sent = json.loads(client.calls[0]["messages"][0]["content"])
    assert [i["state"]["n"] for i in sent["items"]] == ["first", "second"]


def test_every_haiku_call_is_counted_by_the_spend_guard(monkeypatch):
    client = FakeClient([None, [("hype", 0.5)]])
    _use_client(monkeypatch, client)
    haiku.answer(FORMAT_Q, {})
    assert anthropic_surface._calls["decisions"] == 2


def test_spend_cap_falls_back_to_rules_without_calling_anthropic(monkeypatch):
    _explicit_haiku(monkeypatch)
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
    _use_client(monkeypatch, FakeClient([[("hype", 0.8)]]))
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


def test_auto_never_selects_haiku_even_with_anthropic_key(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "sk")
    assert select_backend() == "rules"


def test_auto_selects_jev_only_with_key_and_transport(monkeypatch):
    monkeypatch.setattr(settings, "typesafe_api_key", "ts")
    assert select_backend() == "rules"  # key but no transport wired
    monkeypatch.setattr(jev, "default_transport", lambda: FakeTransport())
    assert select_backend() == "jev"
    monkeypatch.setattr(settings, "typesafe_api_key", None)
    assert select_backend() == "rules"


def test_select_explicit_without_credentials_falls_back_to_rules(monkeypatch):
    monkeypatch.setattr(settings, "decisions_backend", "jev")
    assert select_backend() == "rules"
    monkeypatch.setattr(settings, "decisions_backend", "haiku")
    assert select_backend() == "rules"


def test_select_explicit_haiku_ignores_jev_key(monkeypatch):
    _explicit_haiku(monkeypatch)
    monkeypatch.setattr(settings, "typesafe_api_key", "ts")
    assert select_backend() == "haiku"


def test_ask_uses_the_selected_backend(monkeypatch):
    monkeypatch.setitem(
        BACKENDS,
        "rules",
        Backend(lambda q, s: Answer(value=1, probability=1.0, backend="rules"), lambda: True),
    )
    assert ask(ScoreQuestion(prompt="s", lo=0, hi=1), {}).value == 1


# --- prefilter + classification ---


def _fake_ask_many(probabilities):
    def _ask_many(items):
        answers = []
        for _question, state in items:
            p = probabilities[state["candidate"]["id"]]
            answers.append(
                Answer(value=p >= 0.5, probability=p if p >= 0.5 else 1 - p, backend="rules")
            )
        return answers

    return _ask_many


def test_prefilter_splits_on_include_probability(monkeypatch):
    monkeypatch.setattr(settings, "prefilter_min_probability", 0.35)
    cands = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
    kept, dropped = prefilter_candidates(
        cands, ask_many=_fake_ask_many({"a": 0.9, "b": 0.2, "c": 0.4})
    )
    assert [c["id"] for c in kept] == ["a", "c"]
    assert dropped == [{"id": "b", "prefilter_probability": 0.2}]
    assert "prefilter_probability" not in kept[0]


def test_prefilter_with_rules_keeps_everything(monkeypatch):
    monkeypatch.setattr(settings, "decisions_backend", "rules")
    cands = [{"id": "a", "event_type": "audio_peak"}, {"id": "b"}]
    kept, dropped = prefilter_candidates(cands)
    assert kept == cands
    assert dropped == []


def test_classify_formats_uses_one_batched_ask(monkeypatch):
    monkeypatch.setattr(settings, "decisions_backend", "rules")
    clips = [{"event": "ace"}, {"event": "funny_callout"}, {"event": "zzz"}]
    assert classify_formats(clips) == [route_clip(c)[0] for c in clips]
    sizes = []

    def counting(items):
        sizes.append(len(items))
        return [Answer(value=None, probability=0.0, backend="rules")] * len(items)

    assert classify_formats(clips, ask_many=counting) == ["skipped"] * 3
    assert sizes == [3]


# --- persistence of dropped candidates ---


def test_compute_candidates_keeps_dropped_rows_flagged_in_metadata(monkeypatch):
    from ai_video_editor.candidates import service

    monkeypatch.setattr(service, "get_duration_seconds", lambda _p: 26.0)
    monkeypatch.setattr(
        service,
        "prefilter_candidates",
        lambda rows: ([], [{**r, "prefilter_probability": 0.1} for r in rows]),
    )
    rows, diag = service.compute_candidates({"id": "v1", "path": "x.mp4", "game": None})
    assert diag["prefilter_dropped"] == 1
    assert rows[0]["metadata"]["prefilter_dropped"] is True
    assert rows[0]["metadata"]["prefilter_probability"] == 0.1
    assert "prefilter_probability" not in rows[0]


def test_build_highlights_lists_dropped_candidates_in_index_json_and_md(monkeypatch, tmp_path):
    from ai_video_editor import highlights

    monkeypatch.setattr(settings, "workspace_dir", tmp_path)
    monkeypatch.setattr(highlights, "get_duration_seconds", lambda _p: 100.0)
    monkeypatch.setattr(highlights, "trim_clip", lambda *a, **k: (True, None))
    monkeypatch.setattr(highlights, "_vlm_enabled", lambda: False)
    asset = {"id": "v1", "path": "x.mp4", "filename": "Valorant_05-15-2026_21-19-8-0.mp4"}
    base = {"source": "audio_peak", "event_type": "audio_peak", "start_seconds": 1.0}
    kept = {**base, "id": "c1", "end_seconds": 9.0, "metadata": {}}
    dropped = {
        **base,
        "id": "c2",
        "start_seconds": 30.0,
        "end_seconds": 35.0,
        "metadata": {"prefilter_dropped": True, "prefilter_probability": 0.2},
    }
    rankings = [
        {
            "candidate_id": "c1",
            "keep": True,
            "suggested_start_seconds": 1.0,
            "suggested_end_seconds": 9.0,
        }
    ]
    summary = highlights.build_highlights(asset, rankings, [kept, dropped])
    folder = settings.workspace_dir / summary["folder"]
    written = json.loads((folder / "index.json").read_text("utf-8"))
    assert len(written["clips"]) == 1
    assert written["dropped"] == [
        {
            "candidate_id": "c2",
            "source": "audio_peak",
            "start_seconds": 30.0,
            "end_seconds": 35.0,
            "event_type": "audio_peak",
            "prefilter_probability": 0.2,
        }
    ]
    assert "Dropped by pre-filter (1)" in (folder / "index.md").read_text("utf-8")


# --- jev failures + ranker spend cap ---


class RaisingTransport:
    def choice(self, *a):
        raise ConnectionError("down")

    score = bool = choice


def test_jev_transport_error_is_wrapped_and_ask_falls_back_to_rules(monkeypatch):
    monkeypatch.setattr(settings, "typesafe_api_key", "ts")
    monkeypatch.setattr(jev, "default_transport", lambda: RaisingTransport())
    assert select_backend() == "jev"
    with pytest.raises(decisions.DecisionError):
        jev.answer(FORMAT_Q, {}, transport=RaisingTransport())
    answer = ask(FORMAT_Q, {"clip": {"event": "ace"}})
    assert answer.backend == "rules"
    assert answer.value == Format.hype


def test_jev_unknown_enum_value_is_wrapped():
    class BadValue(FakeTransport):
        def choice(self, prompt, options, state):
            return "not_a_format", 0.9

    with pytest.raises(decisions.DecisionError):
        jev.answer(FORMAT_Q, {}, transport=BadValue())


def test_ranker_spend_cap_raises_before_any_api_call(monkeypatch):
    from ai_video_editor import ranker

    monkeypatch.setattr(settings, "anthropic_max_rank_calls", 0)
    monkeypatch.setattr(
        ranker, "make_client", lambda: pytest.fail("Anthropic client must not be created")
    )
    with pytest.raises(anthropic_surface.SpendCapError, match="rank-call cap reached"):
        ranker.rank_candidates("valorant", [{"id": "c1"}])
