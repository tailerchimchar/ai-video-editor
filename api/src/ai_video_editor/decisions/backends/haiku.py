"""Haiku backend: answers in batches, one `messages.parse` call per chunk.

Cost-happy: N candidates cost ceil(N / DECISIONS_BATCH_SIZE) calls, not N.
The response schema is derived from the question (choice -> Literal of
the enum values, bool, score -> bounded int) and returned as a list with
one entry per item, matched by index. Each call is counted by the spend
guard and traced with `backend` as metadata. A malformed reply is
retried once, then reported as a `DecisionError` so `ask_many` can fall
back to rules. Only used when DECISIONS_BACKEND is explicitly "haiku".
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
_TOKENS_PER_ITEM = 48

# Frozen so it forms a stable cacheable prefix; the question and states
# go in the user turn.
_SYSTEM_PROMPT = """\
You are a decision engine inside a gameplay-video editor. You are given a
question and a numbered list of JSON `items`, each describing one clip or
candidate moment. You never see the video. Answer the question for every
item from its state alone.

Reply with one result per item, echoing its `index`. Each result has
`value` (the answer) and `probability`: your calibrated confidence from
0.0 to 1.0 that `value` is correct. Use the extremes when the state is
clear; say 0.5 or lower when it is not."""


def _batch_model(question: TypedQuestion) -> type[BaseModel]:
    """Response schema for a batch of this question."""
    if isinstance(question, ChoiceQuestion):
        value_type = Literal[tuple(member.value for member in question.options)]
    elif isinstance(question, BoolQuestion):
        value_type = bool
    else:
        value_type = Annotated[int, Field(ge=question.lo, le=question.hi)]
    item = create_model(
        "DecisionItem",
        index=(int, ...),
        value=(value_type, ...),
        probability=(Annotated[float, Field(ge=0.0, le=1.0)], ...),
    )
    return create_model("DecisionBatch", results=(list[item], ...))


def _user_turn(question: TypedQuestion, states: list[dict]) -> str:
    items = [{"index": i, "state": state} for i, state in enumerate(states)]
    return json.dumps({"question": question.prompt, "items": items}, sort_keys=True, default=str)


def _to_answers(question: TypedQuestion, parsed: BaseModel, count: int) -> list[Answer] | None:
    """None when the reply does not cover exactly indices 0..count-1."""
    by_index = {r.index: r for r in parsed.results}
    if len(parsed.results) != count or set(by_index) != set(range(count)):
        return None
    answers = []
    for i in range(count):
        r = by_index[i]
        value = question.options(r.value) if isinstance(question, ChoiceQuestion) else r.value
        answers.append(Answer(value=value, probability=r.probability, backend="haiku"))
    return answers


def _call(client, question: TypedQuestion, states: list[dict], output_model: type[BaseModel]):
    """One counted API call; returns the raw response."""
    try:
        reserve_call("decisions", settings.decisions_max_calls, "DECISIONS_MAX_CALLS")
    except SpendCapError as exc:
        raise DecisionError(str(exc)) from exc
    return client.messages.parse(
        model=settings.ranker_model,
        max_tokens=256 + _TOKENS_PER_ITEM * len(states),
        system=[
            {
                "type": "text",
                "text": _SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": _user_turn(question, states)}],
        output_format=output_model,
    )


@observe(name="typed-decision-batch", as_type="generation")
def _answer_chunk(client, question: TypedQuestion, states: list[dict]) -> list[Answer]:
    output_model = _batch_model(question)
    for _attempt in range(1 + _MALFORMED_RETRIES):
        try:
            response = _call(client, question, states, output_model)
        except ValidationError:
            continue
        except anthropic.APIError as exc:
            raise DecisionError(f"haiku call failed: {exc}") from exc
        answers = (
            _to_answers(question, response.parsed_output, len(states))
            if response.parsed_output is not None
            else None
        )
        if answers is None:
            continue
        record_generation(
            response,
            trace_input={"prompt": question.prompt, "items": len(states)},
            trace_output={"answered": len(answers)},
            tags=["typed-decision"],
            metadata={"backend": "haiku"},
        )
        return answers
    raise DecisionError("haiku returned a malformed reply twice")


def _group_by_question(
    items: list[tuple[TypedQuestion, dict]],
) -> list[tuple[TypedQuestion, list[int]]]:
    """Positions of `items` sharing an identical question (one call each)."""
    groups: list[tuple[TypedQuestion, list[int]]] = []
    for position, (question, _state) in enumerate(items):
        for known, positions in groups:
            if known == question:
                positions.append(position)
                break
        else:
            groups.append((question, [position]))
    return groups


def answer_many(items: list[tuple[TypedQuestion, dict]]) -> list[Answer]:
    client = make_client()
    size = settings.decisions_batch_size
    answers: list[Answer | None] = [None] * len(items)
    for question, positions in _group_by_question(items):
        for start in range(0, len(positions), size):
            chunk = positions[start : start + size]
            states = [items[p][1] for p in chunk]
            results = _answer_chunk(client, question, states)
            for position, result in zip(chunk, results, strict=True):
                answers[position] = result
    return answers


def answer(question: TypedQuestion, state: dict) -> Answer:
    return answer_many([(question, state)])[0]
