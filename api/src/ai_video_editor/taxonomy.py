"""The one vocabulary for classifying clips and moments.

Two rule-based classifiers and the VLM schemas used to keep their own
string lists. They are different axes, so they get different enums:

- `Format`  -- the editorial bucket a clip is routed to (`bucketing.py`).
- `Topic`   -- what happened in the clip (`shorts.categorize_clips`).
- `Phase`   -- when in the game it happened (`shorts.categorize_clips`).

All enums are `StrEnum`: members compare equal to, hash like, and
serialise as their plain string value, so JSON shapes and MCP outputs
do not change. `LEGACY_ALIASES` keeps the pre-taxonomy slugs resolvable.
"""

from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    """`enum.StrEnum` for Python 3.10: str()/format() give the plain value."""

    __str__ = str.__str__


class Game(StrEnum):
    valorant = "valorant"
    league = "league"


class Role(StrEnum):
    """Lane role in League of Legends."""

    top = "top"
    jungle = "jungle"
    mid = "mid"
    adc = "adc"
    support = "support"


class Format(StrEnum):
    """Editorial bucket (mentor taxonomy)."""

    hype = "hype"
    story = "story"
    voice_over = "voice_over"
    funny = "funny"
    arc_montage = "arc_montage"
    commentary = "commentary"  # voice-only story material; never a short on its own


ClipFormat = StrEnum("ClipFormat", {f.name: f.value for f in Format if f is not Format.commentary})
"""Formats Jev may assign to a clip. Commentary is assigned by `commentary.py`
from owner-only speech: a clip with an on-screen event is never commentary.
Members compare equal to the matching `Format` (both are str)."""


PLAN_FORMAT_BY_FORMAT: dict[Format, str] = {
    Format.hype: "hype_montage",
    Format.story: "story_short",
    Format.voice_over: "vo_short",
    Format.funny: "funny_short",
    Format.arc_montage: "arc_montage",
}
"""Editorial bucket -> the edit plan `format` the editing-expert emits for it."""


class Topic(StrEnum):
    """What the clip shows."""

    first_blood = "first_blood"
    outplay = "outplay"
    multikill = "multikill"
    teamfight = "teamfight"
    objective_steal = "objective_steal"
    kite_escape = "kite_escape"
    tower_dive = "tower_dive"
    teamfight_turn = "teamfight_turn"


class Phase(StrEnum):
    """Stage of the game, by anchor time."""

    laning = "laning"
    mid = "mid"
    late = "late"


class PayoffType(StrEnum):
    """What a VLM-identified payoff moment is."""

    kill = "kill"
    multi_kill = "multi_kill"
    ace = "ace"
    clutch = "clutch"
    plant = "plant"
    defuse = "defuse"
    save = "save"
    outplay = "outplay"
    other = "other"
    first_blood = "first_blood"
    multi_kill_2 = "multi_kill_2"
    multi_kill_3 = "multi_kill_3"
    multi_kill_4 = "multi_kill_4"
    multi_kill_5 = "multi_kill_5"
    objective_steal = "objective_steal"
    kite_escape = "kite_escape"
    tower_dive = "tower_dive"
    teamfight_turn = "teamfight_turn"


EVENT_ALIASES: dict[str, PayoffType] = {
    "doublekill": PayoffType.multi_kill_2,
    "triplekill": PayoffType.multi_kill_3,
    "quadrakill": PayoffType.multi_kill_4,
    "pentakill": PayoffType.multi_kill_5,
    "ace": PayoffType.multi_kill_5,
}
"""Game-specific event names (League's Riot names, Valorant's ace) -> the one
canonical kill event. The only place kill vocabulary is spelled out."""

_KILLS_BY_EVENT: dict[str, int] = {
    PayoffType.kill: 1,
    PayoffType.multi_kill_2: 2,
    PayoffType.multi_kill_3: 3,
    PayoffType.multi_kill_4: 4,
    PayoffType.multi_kill_5: 5,
}


def canonical_event(event: str | None) -> str:
    """Lower-cased event name with game aliases folded to their PayoffType."""
    name = (event or "").lower()
    return EVENT_ALIASES.get(name, name)


def kills_for_event(event: str | None) -> int:
    """How many kills an event stands for; 0 for any non-kill event."""
    return _KILLS_BY_EVENT.get(canonical_event(event), 0)


class FlagCategory(StrEnum):
    """What a montage critic flags on an interval. Tuned for gameplay
    critique; adding a member is safe, nothing branches on the value."""

    dead_air = "dead_air"  # walking, empty hallway, nothing happens
    scoreboard = "scoreboard"  # post-round KDA / spectator board
    own_death = "own_death"  # player dies (killfeed shows them as victim)
    teammate_kill_only = "teammate_kill_only"  # killfeed shows a teammate's kill
    payoff_lands = "payoff_lands"  # user's kill / plant / clutch actually happens
    good_buildup = "good_buildup"  # peek prep, angle hold, ability setup
    great_moment = "great_moment"  # ace / multikill / clutch -- flag as a keeper
    abrupt_cut = "abrupt_cut"  # transition into the next clip feels wrong
    boring_pacing = "boring_pacing"  # nothing wrong but low energy for too long
    kill_anchor_misplaced = "kill_anchor_misplaced"  # killfeed pop straddles a cut
    killfeed_pop_cut_off = "killfeed_pop_cut_off"  # killfeed row cut before it draws
    cut_too_early = "cut_too_early"  # sub-clip ends before the payoff resolves
    cut_too_late = "cut_too_late"  # dead frames after the payoff


# Named bands over the 0-1 intensity scale: (exclusive upper bound, name).
_INTENSITY_BANDS = ((0.35, "low"), (0.65, "mid"), (0.85, "high"), (1.01, "peak"))


class Intensity(float):
    """How intense a moment is, 0.0 to 1.0, with a named `band`."""

    def __new__(cls, value: float) -> Intensity:
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"intensity must be within 0-1, got {value}")
        return super().__new__(cls, value)

    @property
    def band(self) -> str:
        return next(name for bound, name in _INTENSITY_BANDS if self < bound)


# Pre-taxonomy slug -> its one taxonomy member. The time-threshold slugs
# become Phases; every other slug is the same word as its enum member.
_LEGACY_FORMATS = (Format.hype, Format.story, Format.voice_over, Format.funny)
_LEGACY_TOPICS = (
    Topic.first_blood,
    Topic.outplay,
    Topic.multikill,
    Topic.teamfight,
    Topic.objective_steal,
)

LEGACY_ALIASES: dict[str, Format | Topic | Phase] = {
    **{m.value: m for m in (*_LEGACY_FORMATS, *_LEGACY_TOPICS)},
    "laning_phase": Phase.laning,
    "mid_game": Phase.mid,
    "late_game": Phase.late,
}

LEGACY_SLUGS: dict[Format | Topic | Phase, str] = {m: slug for slug, m in LEGACY_ALIASES.items()}
