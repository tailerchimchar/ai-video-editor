"""Backend registry. A new backend is one new file here plus one entry.

A backend is `answer(question, state) -> Answer` plus `available() -> bool`
(credentials present) and may also supply
`answer_many(items) -> list[Answer]` to answer a batch in one call (the
default loops `answer`). Both raise `DecisionError` when they cannot
answer.
"""

from collections.abc import Callable
from dataclasses import dataclass

from ..models import Answer, TypedQuestion
from . import haiku, jev, rules

Items = list[tuple[TypedQuestion, dict]]


@dataclass(frozen=True)
class Backend:
    answer: Callable[[TypedQuestion, dict], Answer]
    available: Callable[[], bool]
    answer_many: Callable[[Items], list[Answer]] | None = None

    def many(self, items: Items) -> list[Answer]:
        if self.answer_many is not None:
            return self.answer_many(items)
        return [self.answer(question, state) for question, state in items]


BACKENDS: dict[str, Backend] = {
    "jev": Backend(jev.answer, jev.available, jev.answer_many),
    "haiku": Backend(haiku.answer, haiku.available, haiku.answer_many),
    "rules": Backend(rules.answer, rules.available),
}
