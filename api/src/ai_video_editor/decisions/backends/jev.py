"""Jev backend: TypeSafe's System One model over its HTTP API.

One Jev request evaluates many questions against one state, in parallel
and in isolation, so a batch of `(question, state)` items becomes one
request per distinct state carrying every question asked about it.
Our question kinds map onto Jev primitives:

- `ChoiceQuestion` -> `choice` (criteria = option name -> description)
- `BoolQuestion`   -> `noul`   (probability the statement is true)
- `ScoreQuestion`  -> `score`  (ordered rubric, one level per integer)

Jev returns calibrated probabilities, not text, so `Answer.probability`
is the probability Jev assigned to the value we return. The transport is
a one-method protocol (`ask(state, questions) -> answers`) so tests use a
fake and the real one is a thin `httpx` POST.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

import httpx

from ...anthropic_surface import SpendCapError, reserve_call
from ...config import settings
from ..models import (
    Answer,
    BoolQuestion,
    ChoiceQuestion,
    DecisionError,
    ScoreQuestion,
    TypedQuestion,
)

_log = logging.getLogger(__name__)

_SYSTEM_ONE_PATH = "/v1/systemone"


_IMAGES_HEADER = "TypeSafe-Danger-Zone-Images"


class JevTransport(Protocol):
    """Send one state plus its questions (System One JSON), get the raw
    `answers` object back, keyed like the questions.

    Optional `assets` turns on image support (header + top-level assets map).
    Omit it for text-only requests — those stay unchanged.
    """

    def ask(
        self,
        state: dict,
        questions: dict[str, dict],
        *,
        assets: dict[str, dict] | None = None,
    ) -> dict[str, dict]: ...


class HttpTransport:
    """`POST {base_url}/v1/systemone` with bearer auth.

    Pass `assets` only when the state/questions reference images. That adds
    the Danger-Zone header and the top-level `assets` map. Text-only calls
    (no assets) keep the same body and headers as before.
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.model = model or settings.typesafe_model
        self._url = (base_url or settings.typesafe_base_url).rstrip("/") + _SYSTEM_ONE_PATH
        self._client = client or httpx.Client(timeout=timeout or settings.typesafe_timeout_seconds)
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self.last_usage: dict[str, Any] = {}

    def ask(
        self,
        state: dict,
        questions: dict[str, dict],
        *,
        assets: dict[str, dict] | None = None,
    ) -> dict[str, dict]:
        body: dict[str, Any] = {"model": self.model, "state": state, "questions": questions}
        headers = self._headers
        if assets:
            body["assets"] = assets
            headers = {**self._headers, _IMAGES_HEADER: "true"}
        response = self._client.post(self._url, json=body, headers=headers)
        if response.status_code != 200:
            raise DecisionError(f"jev HTTP {response.status_code}: {_error_detail(response)}")
        payload = response.json()
        usage = payload.get("usage") or {}
        self.last_usage = usage if isinstance(usage, dict) else {}
        _log.debug(
            "jev %s answered %d questions (%s input tokens)",
            payload.get("model"),
            len(questions),
            usage.get("input_tokens") if isinstance(usage, dict) else None,
        )
        return payload["answers"]


def _error_detail(response: httpx.Response) -> str:
    try:
        detail = response.json().get("detail")
    except ValueError:
        return response.text[:200]
    if isinstance(detail, list):
        return "; ".join(str(item.get("msg", item)) for item in detail)
    return str(detail)


def default_transport() -> JevTransport | None:
    """The HTTP transport when TYPESAFE_API_KEY is set, else None."""
    if not settings.typesafe_api_key:
        return None
    return HttpTransport(settings.typesafe_api_key)


def available() -> bool:
    return bool(settings.typesafe_api_key)


# --- mapping: our typed questions <-> Jev primitives ---


def to_jev_question(question: TypedQuestion) -> dict:
    if isinstance(question, ChoiceQuestion):
        descriptions = question.descriptions or {}
        criteria = {m.value: descriptions.get(m.value, m.value) for m in question.options}
        return {"type": "choice", "instructions": question.prompt, "criteria": criteria}
    if isinstance(question, BoolQuestion):
        return {"type": "noul", "instructions": question.prompt}
    if isinstance(question, ScoreQuestion):
        levels = [str(level) for level in range(question.lo, question.hi + 1)]
        return {"type": "score", "instructions": question.prompt, "criteria": levels}
    raise DecisionError(f"unsupported question: {type(question).__name__}")


def from_jev_answer(question: TypedQuestion, raw: dict[str, Any]) -> Answer:
    expected = to_jev_question(question)["type"]
    if raw.get("type") != expected:
        raise DecisionError(f"jev answered type {raw.get('type')!r}, expected {expected!r}")
    probabilities = raw.get("probabilities") or {}
    if isinstance(question, ChoiceQuestion):
        try:
            value = question.options(raw["choice"])
        except (KeyError, ValueError) as exc:
            raise DecisionError(f"jev choice {raw.get('choice')!r} is not an option") from exc
        probability = probabilities.get(value.value, raw.get("confidence", 0.0))
    elif isinstance(question, BoolQuestion):
        p_true = float(raw["noul"])
        value = p_true >= 0.5
        probability = p_true if value else 1.0 - p_true
    else:
        level = min(max(round(float(raw["score"])), 0), question.hi - question.lo)
        value = question.lo + level
        probability = probabilities.get(str(level), raw.get("confidence", 0.0))
    return Answer(value=value, probability=float(probability), backend="jev")


# --- batching: one request per distinct state ---


def _group_by_state(
    items: list[tuple[TypedQuestion, dict]],
) -> list[tuple[dict, list[int]]]:
    """Positions of `items` sharing an identical state (one request each)."""
    groups: list[tuple[dict, list[int]]] = []
    for position, (_question, state) in enumerate(items):
        for known, positions in groups:
            if known == state:
                positions.append(position)
                break
        else:
            groups.append((state, [position]))
    return groups


Items = list[tuple[TypedQuestion, dict]]


def _ask_state(
    transport: JevTransport, items: Items, state: dict, positions: list[int]
) -> list[Answer]:
    try:
        reserve_call("jev", settings.typesafe_max_calls, "TYPESAFE_MAX_CALLS")
    except SpendCapError as exc:
        raise DecisionError(str(exc)) from exc
    questions = {f"q{i}": to_jev_question(items[p][0]) for i, p in enumerate(positions)}
    try:
        raw_answers = transport.ask(state, questions)
    except DecisionError:
        raise
    except Exception as exc:  # a transport must never crash the pipeline
        raise DecisionError(f"jev call failed: {exc}") from exc
    answers = []
    for key, position in zip(questions, positions, strict=True):
        if key not in raw_answers:
            raise DecisionError(f"jev reply is missing question {key!r}")
        answers.append(from_jev_answer(items[position][0], raw_answers[key]))
    return answers


def answer_many(items: Items, transport: JevTransport | None = None) -> list[Answer]:
    transport = transport or default_transport()
    if transport is None:
        raise DecisionError("Jev is not configured: set TYPESAFE_API_KEY")
    answers: list[Answer | None] = [None] * len(items)
    for state, positions in _group_by_state(items):
        results = _ask_state(transport, items, state, positions)
        for position, result in zip(positions, results, strict=True):
            answers[position] = result
    return answers  # type: ignore[return-value]


def answer(question: TypedQuestion, state: dict, transport: JevTransport | None = None) -> Answer:
    return answer_many([(question, state)], transport=transport)[0]
