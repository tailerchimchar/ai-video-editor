"""Rules backend: free, offline, deterministic. The default and the
fallback when an LLM backend is unavailable or fails.

Format questions reuse the mentor routing rules in `bucketing`. It has
no opinion on questions it has no rule for (value None, probability 0),
and includes every candidate in the pre-filter.
"""

from ...taxonomy import Format, Topic, kills_for_event
from ..models import Answer, BoolQuestion, ChoiceQuestion, TypedQuestion

_TOPIC_BY_EVENT = {
    "objective": Topic.objective_steal,
    "clutch": Topic.outplay,
}
"""Non-kill events with a topic; any 2K-and-up kill event is a multikill."""


def _topic(event: str) -> Topic | None:
    if kills_for_event(event) >= 2:
        return Topic.multikill
    return _TOPIC_BY_EVENT.get(event)


_NO_OPINION = Answer(value=None, probability=0.0, backend="rules")


def _event(state: dict) -> str:
    clip = state.get("clip") or {}
    return (clip.get("event") or clip.get("event_type") or "").lower()


def _choose(question: ChoiceQuestion, state: dict) -> Answer:
    if question.options is Format:
        # Imported here: bucketing depends on `decisions`, not the reverse.
        from ...bucketing import route_clip

        bucket, _reason = route_clip(state.get("clip") or {})
        return Answer(value=bucket, probability=1.0, backend="rules")
    topic = _topic(_event(state)) if question.options is Topic else None
    if topic is not None:
        return Answer(value=topic, probability=1.0, backend="rules")
    return _NO_OPINION


def available() -> bool:
    return True


def answer(question: TypedQuestion, state: dict) -> Answer:
    if isinstance(question, ChoiceQuestion):
        return _choose(question, state)
    if isinstance(question, BoolQuestion):
        return Answer(value=True, probability=1.0, backend="rules")
    return _NO_OPINION
