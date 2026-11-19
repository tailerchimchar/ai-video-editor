"""Backend registry. A new backend is one new file here plus one entry.

Each backend is `answer(question, state) -> Answer` and raises
`DecisionError` when it cannot answer.
"""

from collections.abc import Callable

from ..models import Answer, TypedQuestion
from . import haiku, jev, rules

Backend = Callable[[TypedQuestion, dict], Answer]

BACKENDS: dict[str, Backend] = {
    "jev": jev.answer,
    "haiku": haiku.answer,
    "rules": rules.answer,
}
