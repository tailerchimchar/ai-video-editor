# Task 2 stub of the Task 1 interface; the Task 1 version replaces this on merge.
"""Game model: phases, plays and objectives (spec 4.1-4.3)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class _M(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Phases(_M):
    early: dict = Field(default_factory=dict)
    mid: dict = Field(default_factory=dict)
    late: dict = Field(default_factory=dict)


class ObjectiveEvent(_M):
    type: str = ""
    game_time_s: float = 0.0


class PlayTimes(_M):
    first_kill_s: float = 0.0
    last_kill_s: float = 0.0


class PlayWindow(_M):
    story_start_s: float = 0.0
    montage_start_s: float = 0.0
    end_s: float = 0.0


class PlaySides(_M):
    allies: list[str] = Field(default_factory=list)
    enemies: list[str] = Field(default_factory=list)
    numbers: str = ""
    numbers_source: str = "riot_lower_bound"
    numbers_confirmed: bool = False


class PlayOwner(_M):
    involved: bool = False
    role: str = "absent"
    kills: int = 0
    assists: int = 0
    died: bool = False


class PlayResult(_M):
    enemy_deaths: int = 0
    ally_deaths: int = 0
    outcome: str = ""
    objective_after: list[str] = Field(default_factory=list)
    building_after: str | None = None


class PlayFlags(_M):
    model_config = ConfigDict(extra="allow")


class Impressive(_M):
    numbers_faced: int = 0
    rank: int = 0


class Play(_M):
    id: str
    type: str
    phase: str = ""
    game_time: PlayTimes = Field(default_factory=PlayTimes)
    recording_time: PlayTimes = Field(default_factory=PlayTimes)
    window: PlayWindow = Field(default_factory=PlayWindow)
    zone: str = ""
    zone_relative: str = ""
    position: dict | None = None
    sides: PlaySides = Field(default_factory=PlaySides)
    owner: PlayOwner = Field(default_factory=PlayOwner)
    result: PlayResult = Field(default_factory=PlayResult)
    flags: PlayFlags = Field(default_factory=PlayFlags)
    impressive: Impressive = Field(default_factory=Impressive)
    vocab_hash: str = ""
    text: str = ""


class GameModel(_M):
    version: int = 3
    phases: Phases | None = None
    plays: list[Play] = Field(default_factory=list)
    objectives: list[ObjectiveEvent] = Field(default_factory=list)
    vocab_hash: str = ""
    budget_spent_usd: float = 0.0
