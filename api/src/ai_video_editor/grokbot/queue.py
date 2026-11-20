"""Queue items cloud bots write into the git repo. All content is hostile."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from ..edit_plan import EditPlan

_HOSTILE = re.compile(
    r"https?://|[A-Za-z]:\\|(?:^|[\s\"'(=])/[^\s/]+/|`|\$\(|;|\|",
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Envelope(_Strict):
    id: str
    kind: str
    author_bot: Literal["lead", "editor", "devops"]
    created: str
    schema_version: Literal[1]


class CopyBlock(_Strict):
    title: str
    caption: str
    hashtags: list[str]
    thumbnail_t: float | None = None


class StrictEditPlan(EditPlan):
    model_config = ConfigDict(extra="forbid")


class EditPlanItem(Envelope):
    kind: Literal["edit_plan"]
    compilation_id: str
    base_version: str
    plan: StrictEditPlan
    copy: CopyBlock | None = None


class PreviewRequestItem(Envelope):
    kind: Literal["preview_request"]
    asset_id: str | None = None
    compilation_id: str | None = None
    start_s: float
    end_s: float
    question: str

    @model_validator(mode="after")
    def _check(self) -> PreviewRequestItem:
        if (self.asset_id is None) == (self.compilation_id is None):
            raise ValueError("exactly one of asset_id/compilation_id required")
        if not 0 <= self.start_s < self.end_s:
            raise ValueError("need 0 <= start_s < end_s")
        if self.end_s - self.start_s > 20:
            raise ValueError("window longer than 20 seconds")
        return self


class VerdictItem(Envelope):
    kind: Literal["verdict"]
    game: str
    clip_ref: str
    verdict: Literal["payoff", "false_positive", "style"]
    rule: str
    quote: str = ""
    quote_ts: str | None = None


class DeployItem(Envelope):
    kind: Literal["deploy"]
    tag: str = Field(pattern=r"^v\d+\.\d+\.\d+$")


class SleepItem(Envelope):
    kind: Literal["sleep"]


class WakeItem(Envelope):
    kind: Literal["wake"]


QueueItem = Annotated[
    EditPlanItem | PreviewRequestItem | VerdictItem | DeployItem | SleepItem | WakeItem,
    Field(discriminator="kind"),
]

_ADAPTER: TypeAdapter = TypeAdapter(QueueItem)


class QueueRejected(ValueError):  # noqa: N818
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _walk_strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _walk_strings(k)
            yield from _walk_strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _walk_strings(v)


def parse_item(text: str) -> QueueItem:
    try:
        data = json.loads(text)
    except (ValueError, TypeError) as e:
        raise QueueRejected(f"invalid JSON: {e}") from e
    try:
        item = _ADAPTER.validate_python(data)
    except ValidationError as e:
        raise QueueRejected(f"validation failed: {e}") from e
    for s in _walk_strings(item.model_dump()):
        if _HOSTILE.search(s):
            raise QueueRejected(f"hostile string rejected: {s[:40]!r}")
    if isinstance(item, EditPlanItem):
        if any(e.at != 0.0 for e in item.plan.effects):
            raise QueueRejected("timed effects are not applied yet")
        if item.plan.cards or item.plan.sfx:
            raise QueueRejected("cards/sfx are not supported yet (preflight refuses them)")
    return item


_LEDGER_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS queue_applied ("
    "id TEXT PRIMARY KEY, kind TEXT NOT NULL, "
    "applied_at TEXT NOT NULL, outcome TEXT NOT NULL)"
)


class AppliedLedger:
    def __init__(self, db_path: Path):
        self._path = Path(db_path)
        self._run(lambda c: c.execute(_LEDGER_SCHEMA))

    def _run(self, fn):
        conn = sqlite3.connect(self._path)
        try:
            with conn:
                return fn(conn)
        finally:
            conn.close()

    def seen(self, id: str) -> bool:
        return (
            self._run(
                lambda c: c.execute("SELECT 1 FROM queue_applied WHERE id=?", (id,)).fetchone()
            )
            is not None
        )

    def mark(self, id: str, kind: str, outcome: str) -> None:
        self._run(
            lambda c: c.execute(
                "INSERT OR IGNORE INTO queue_applied (id, kind, applied_at, outcome) "
                "VALUES (?,?,?,?)",
                (id, kind, datetime.now(timezone.utc).isoformat(), outcome),
            )
        )

    def outcomes(self) -> dict[str, str]:
        return dict(
            self._run(lambda c: c.execute("SELECT id, outcome FROM queue_applied").fetchall())
        )


def parse_sleep_flag(repo_root: Path) -> bool:
    return (Path(repo_root) / "SLEEP").exists()
