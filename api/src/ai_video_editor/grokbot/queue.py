"""Queue items cloud bots write into the git repo. All content is hostile."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
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
    field_validator,
    model_validator,
)

from ..edit_plan import (
    EditPlan,
    PlanCard,
    PlanEffect,
    PlanMusic,
    PlanSfx,
    Rejected,
    SourceClip,
)

# Strict on purpose: false positives are acceptable. Any backslash is
# rejected; a "/" is only allowed directly after an alphanumeric, "-" or "_"
# (so "and/or" passes, but " /etc", ",/etc", "[/etc", "./etc" and a leading
# "/" do not). Shell characters ; | & > < ` and $( are rejected.
_HOSTILE = re.compile(
    r"https?://|\\|(?<![A-Za-z0-9\-_])/|//|`|\$\(|[;|&<>]",
)
MAX_ITEM_BYTES = 64 * 1024


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Envelope(_Strict):
    id: str
    kind: str
    author_bot: Literal["lead", "editor", "devops"]
    created: str
    schema_version: Literal[1]

    @field_validator("id")
    @classmethod
    def _uuid_id(cls, v: str) -> str:
        # Ids become filenames: only canonical lowercase UUIDs are allowed.
        return str(uuid.UUID(v))


class CopyBlock(_Strict):
    title: str
    caption: str
    hashtags: list[str]
    thumbnail_t: float | None = None


class _StrictSourceClip(SourceClip):
    model_config = ConfigDict(extra="forbid")


class _StrictPlanMusic(PlanMusic):
    model_config = ConfigDict(extra="forbid")


class _StrictPlanEffect(PlanEffect):
    model_config = ConfigDict(extra="forbid")


class _StrictPlanCard(PlanCard):
    model_config = ConfigDict(extra="forbid")


class _StrictPlanSfx(PlanSfx):
    model_config = ConfigDict(extra="forbid")


class _StrictRejected(Rejected):
    model_config = ConfigDict(extra="forbid")


class StrictEditPlan(EditPlan):
    model_config = ConfigDict(extra="forbid")

    source_clips: list[_StrictSourceClip] = Field(default_factory=list)
    music: _StrictPlanMusic | None = None
    effects: list[_StrictPlanEffect] = Field(default_factory=list)
    cards: list[_StrictPlanCard] = Field(default_factory=list)
    sfx: list[_StrictPlanSfx] = Field(default_factory=list)
    rejected: list[_StrictRejected] = Field(default_factory=list)


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

    @property
    def has_quote(self) -> bool:
        """True only for a non-blank quote. Callers must use this, not `quote`."""
        return bool(self.quote.strip())


class DeployItem(Envelope):
    kind: Literal["deploy"]
    tag: str = Field(pattern=r"^v[0-9]+\.[0-9]+\.[0-9]+$")


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


def _walk_strings(root: Any) -> Iterator[str]:
    """Yield every str (dict keys included) without recursion."""
    stack = [root]
    while stack:
        value = stack.pop()
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for k, v in value.items():
                stack.append(k)
                stack.append(v)
        elif isinstance(value, (list, tuple)):
            stack.extend(value)


def parse_item(text: str) -> QueueItem:
    if len(text.encode("utf-8", errors="replace")) > MAX_ITEM_BYTES:
        raise QueueRejected("item too large")
    try:
        data = json.loads(text)
    except RecursionError as e:
        raise QueueRejected("invalid JSON: nested too deeply") from e
    except (ValueError, TypeError) as e:
        raise QueueRejected(f"invalid JSON: {e}") from e
    # Scan the raw decoded JSON so nothing can hide behind pydantic.
    for s in _walk_strings(data):
        if _HOSTILE.search(s):
            raise QueueRejected(f"hostile string rejected: {s[:40]!r}")
    try:
        item = _ADAPTER.validate_python(data)
    except RecursionError as e:
        raise QueueRejected("validation failed: nested too deeply") from e
    except ValidationError as e:
        raise QueueRejected(f"validation failed: {e}") from e
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
                "INSERT INTO queue_applied (id, kind, applied_at, outcome) "
                "VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "outcome=excluded.outcome, applied_at=excluded.applied_at "
                "WHERE queue_applied.outcome='deferred'",
                (id, kind, datetime.now(timezone.utc).isoformat(), outcome),
            )
        )

    def outcomes(self) -> dict[str, str]:
        return dict(
            self._run(lambda c: c.execute("SELECT id, outcome FROM queue_applied").fetchall())
        )


def parse_sleep_flag(repo_root: Path) -> bool:
    return (Path(repo_root) / "SLEEP").is_file()
