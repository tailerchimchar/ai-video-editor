"""Haiku backend: one `messages.parse` call per question.

The response schema is derived from the question (choice -> Literal of
the enum values, bool, score -> bounded int) plus a probability. Each
attempt is counted by the spend guard and traced with `backend` as
metadata. A malformed reply is retried once, then reported as a
`DecisionError` so `ask` can fall back to rules.
"""

import json
from typing import Annotated, Literal

import anthropic
from pydantic import BaseModel, Field, ValidationError, create_model

from ...anthropic_surface import (
    SpendCapError,
    make_client,
    observe,
    record_generation,
    reserve_call,
)
from ...config import settings
from ..models import (
    Answer,
    BoolQuestion,
    ChoiceQuestion,
    DecisionError,
    TypedQuestion,
)

_MALFORMED_RETRIES = 1

# Frozen so it forms a stable cacheable prefix; the question and state
# go in the user turn.
_SYSTEM_PROMPT = """\
You are a decision engine inside a gameplay-video editor. You are given a
question and a JSON `state` describing one clip or candidate moment. You
never see the video. Answer the question from the state alone.

Reply with `value` (the answer) and `probability`: your calibrated
confidence from 0.0 to 1.0 that `value` is correct. Use the extremes when
the state is clear; say 0.5 or lower when it is not."""


def _output_model(question: TypedQuestion) -> type[BaseModel]:
    """Response schema for this question."""
    if isinstance(question, ChoiceQuestion):
        value_type = Literal[tuple(member.value for member in question.options)]
    elif isinstance(question, BoolQuestion):
        value_type = bool
    else:
        value_type = Annotated[int, Field(ge=question.lo, le=question.hi)]
    return create_model(
        "Decision",
        value=(value_type, ...),
        probability=(Annotated[float, Field(ge=0.0, le=1.0)], ...),
    )


def _user_turn(question: TypedQuestion, state: dict) -> str:
    return json.dumps({"question": question.prompt, "state": state}, sort_keys=True, default=str)


def _to_answer(question: TypedQuestion, parsed: BaseModel) -> Answer:
    value = question.options(parsed.value) if isinstance(question, ChoiceQuestion) else parsed.value
    return Answer(value=value, probability=parsed.probability, backend="haiku")


def _call(client, question: TypedQuestion, state: dict, output_model: type[BaseModel]):
    """One counted API call; returns the raw response."""
    try:
        reserve_call("decisions", settings.decisions_max_calls, "DECISIONS_MAX_CALLS")
    except SpendCapError as exc:
        raise DecisionError(str(exc)) from exc
    return client.messages.parse(
        model=settings.ranker_model,
        max_tokens=256,
        system=[
            {
                "type": "text",
                "text": _SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": _user_turn(question, state)}],
        output_format=output_model,
    )


@observe(name="typed-decision", as_type="generation")
def answer(question: TypedQuestion, state: dict) -> Answer:
    client = make_client()
    output_model = _output_model(question)
    for _attempt in range(1 + _MALFORMED_RETRIES):
        try:
            response = _call(client, question, state, output_model)
        except ValidationError:
            continue
        except anthropic.APIError as exc:
            raise DecisionError(f"haiku call failed: {exc}") from exc
        if response.parsed_output is None:
            continue
        result = _to_answer(question, response.parsed_output)
        record_generation(
            response,
            trace_input={"prompt": question.prompt},
            trace_output={"value": str(result.value), "probability": result.probability},
            tags=["typed-decision"],
            metadata={"backend": "haiku"},
        )
        return result
    raise DecisionError("haiku returned a malformed reply twice")
