"""Queue items cloud bots write into the git repo. All content is hostile."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
import warnings
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
from ..taxonomy import Game

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

    # A library id only: never a path, so it can never become an ffmpeg input.
    song_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")


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


# `copy` shadows BaseModel.copy on purpose (the bots' JSON contract); keep
# pydantic's shadow warning out of the runner and API logs.
with warnings.catch_warnings():
    warnings.filterwarnings("ignore", message=r'Field name "copy"', category=UserWarning)

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


_KNOWN_GAMES = frozenset(g.value for g in Game)


class VerdictItem(Envelope):
    kind: Literal["verdict"]
    # Becomes a file name under the mentor hints dir: known games only.
    game: str = Field(pattern=r"^[a-z][a-z0-9_]{1,31}$")
    clip_ref: str
    verdict: Literal["payoff", "false_positive", "style"]
    rule: str
    quote: str = ""
    quote_ts: str | None = None

    @field_validator("game")
    @classmethod
    def _known_game(cls, v: str) -> str:
        if v not in _KNOWN_GAMES:
            raise ValueError(f"unknown game; expected one of {sorted(_KNOWN_GAMES)}")
        return v

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


_MAX_RENDER_CLIPS = 20
_MAX_CLIP_WINDOW_S = 180.0


class RenderClip(_Strict):
    """One clip in a render_request, identified by Drive file id."""

    clip_ref: str = Field(pattern=r"^[1-9][0-9]{0,3}$")
    drive_id: str = Field(pattern=r"^[A-Za-z0-9_-]{10,128}$")
    start_s: float = 0.0
    end_s: float = 0.0

    @model_validator(mode="after")
    def _check_window(self) -> RenderClip:
        if not 0 <= self.start_s < self.end_s:
            raise ValueError("need 0 <= start_s < end_s")
        if self.end_s - self.start_s > _MAX_CLIP_WINDOW_S:
            raise ValueError(f"clip window longer than {_MAX_CLIP_WINDOW_S:.0f} seconds")
        return self


class RenderRequestItem(Envelope):
    """Box render job: render an edit plan from Drive clips.

    Written by the Editor. The box downloads clips by drive_id, renders
    both 9:16 and 16:9 outputs, uploads to Drive, and emits video.rendered.
    """

    kind: Literal["render_request"]
    asset_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    edit_plan_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    edit_plan_version: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,32}$")
    game: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    clips: list[RenderClip] = Field(min_length=1, max_length=_MAX_RENDER_CLIPS)
    concat_order: list[str] = Field(min_length=1, max_length=_MAX_RENDER_CLIPS)
    music_song_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")

    @field_validator("game")
    @classmethod
    def _known_game(cls, v: str) -> str:
        if v not in _KNOWN_GAMES:
            raise ValueError(f"unknown game; expected one of {sorted(_KNOWN_GAMES)}")
        return v

    @model_validator(mode="after")
    def _check_clips(self) -> RenderRequestItem:
        refs = [c.clip_ref for c in self.clips]
        if len(refs) != len(set(refs)):
            raise ValueError("duplicate clip_ref")
        for ref in self.concat_order:
            if ref not in refs:
                raise ValueError(f"concat_order references unknown clip_ref: {ref}")
        return self


QueueItem = Annotated[
    EditPlanItem
    | PreviewRequestItem
    | VerdictItem
    | DeployItem
    | SleepItem
    | WakeItem
    | RenderRequestItem,
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


def _reasons(e: ValidationError, limit: int = 5) -> str:
    """Field locations and messages only: the input is never echoed back."""
    parts = [
        f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']} [{err['type']}]"
        for err in e.errors(include_input=False, include_url=False)[:limit]
    ]
    return "; ".join(parts)


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
        raise QueueRejected(f"validation failed: {_reasons(e)}") from e
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
                lambda c: c.execute(
                    "SELECT 1 FROM queue_applied WHERE id=? AND outcome != 'applying'", (id,)
                ).fetchone()
            )
            is not None
        )

    def mark(self, id: str, kind: str, outcome: str) -> None:
        self._run(
            lambda c: c.execute(
                "INSERT INTO queue_applied (id, kind, applied_at, outcome) "
                "VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "outcome=excluded.outcome, applied_at=excluded.applied_at "
                "WHERE queue_applied.outcome IN ('deferred','applying')",
                (id, kind, datetime.now(timezone.utc).isoformat(), outcome),
            )
        )

    def outcomes(self) -> dict[str, str]:
        return dict(
            self._run(lambda c: c.execute("SELECT id, outcome FROM queue_applied").fetchall())
        )


def parse_sleep_flag(repo_root: Path) -> bool:
    flag = Path(repo_root) / "SLEEP"
    # A symlinked SLEEP is never trusted (the runner refuses to touch it).
    return flag.is_file() and not flag.is_symlink()
