"""Play, phase and game-model data shapes (game understanding, Task 1).

Dependency-free pydantic models so digest and story code can import them
without pulling in config or the Riot cache. Nested models ignore unknown
keys so a richer producer (the example digest) still validates."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

PlayType = Literal[
    "trade", "gank", "dive", "skirmish", "teamfight", "objective_fight", "caught_out", "closeout"
]


class _Tolerant(BaseModel):
    model_config = ConfigDict(extra="ignore")


class PhaseBounds(_Tolerant):
    """Game-second bounds of one phase; `boundary` says what started it
    (late) or ended it (early), e.g. `first_tower`, `14:00`, `first_baron`."""

    name: Literal["early", "mid", "late"]
    start_s: float
    end_s: float
    boundary: str = ""


class Phases(_Tolerant):
    early: PhaseBounds
    mid: PhaseBounds
    late: PhaseBounds | None = None

    @model_validator(mode="before")
    @classmethod
    def _fill_names(cls, data):
        """Accept the example digest shape: no `name`, `ended_by`/`started_by`."""
        if not isinstance(data, dict):
            return data
        out = dict(data)
        for key in ("early", "mid", "late"):
            raw = out.get(key)
            if isinstance(raw, dict):
                raw = dict(raw)
                raw.setdefault("name", key)
                raw.setdefault("boundary", raw.get("ended_by") or raw.get("started_by") or "")
                out[key] = raw
        return out


class PlayTimes(_Tolerant):
    first_kill_s: float
    last_kill_s: float


class PlayWindow(_Tolerant):
    """Recording-second window of a play; filled by the VLM confirmer."""

    approach_s: float | None = None
    engage_s: float | None = None
    outcome_s: float | None = None
    source: Literal["vlm", "fallback", "vlm_pending"] = "vlm_pending"
    offset_correction_s: float | None = None
    flags: list[str] = Field(default_factory=list)


class PlaySides(_Tolerant):
    allies: list[str]
    enemies: list[str]
    numbers: str
    numbers_source: Literal["riot_lower_bound", "vlm_confirmed"] = "riot_lower_bound"
    numbers_confirmed: str | None = None


class PlayOwner(_Tolerant):
    involved: bool
    role: Literal["initiator_kill", "follow_up", "caught_first", "absent"]
    kills: int
    assists: int
    died: bool


class PlayResult(_Tolerant):
    enemy_deaths: int
    ally_deaths: int
    outcome: Literal["won", "lost", "even"]
    objective_after: list[str] = Field(default_factory=list)
    building_after: list[str] = Field(default_factory=list)


class PlayFlags(_Tolerant):
    flank: bool | None = None
    help_from_team: bool
    soul_point: bool | None = None
    steal: bool = False


class Impressive(_Tolerant):
    numbers_faced: int | None = None
    rank: int | None = None


class Play(_Tolerant):
    id: str
    type: PlayType
    phase: str
    game_time: PlayTimes
    recording_time: PlayTimes
    window: PlayWindow
    zone: str
    zone_relative: str = ""
    position: dict | None = None
    sides: PlaySides
    owner: PlayOwner
    result: PlayResult
    flags: PlayFlags
    impressive: Impressive = Field(default_factory=Impressive)
    vocab_hash: str = ""
    text: str = ""


class ObjectiveEvent(_Tolerant):
    t_s: float
    type: str
    sub: str | None = None
    team: Literal["ally", "enemy"]


class GameModel(_Tolerant):
    version: int = 3
    phases: Phases | None = None
    plays: list[Play] = Field(default_factory=list)
    objectives: list[ObjectiveEvent] = Field(default_factory=list)
    vocab_hash: str = ""
    budget_spent_usd: float = 0.0
