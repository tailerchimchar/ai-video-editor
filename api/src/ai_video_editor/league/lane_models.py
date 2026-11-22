"""Lane summary data shapes. Dependency-free so grokbot.digest can import
them without pulling in config or the Riot cache."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class LaneMinute:
    minute: int
    owner_cs: int
    owner_gold: int
    owner_level: int
    opp_cs: int
    opp_gold: int
    opp_level: int


@dataclass
class LaneSummary:
    opponent_champion: str
    minutes: list[LaneMinute] = field(default_factory=list)
    cs_diff_at_10: int | None = None
    gold_diff_at_10: int | None = None
    first_blood_s: float | None = None
    first_lane_kill_s: float | None = None
    owner_first_blood: bool = False
