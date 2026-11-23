# Task 2 stub of the Task 1 interface; the Task 1 version replaces this on merge.
"""Play helpers."""

from __future__ import annotations

from .play_models import Play


def play_text(play: Play) -> str:
    """R4: templated from facts only, so a play can never trip the leak check."""
    r = play.result
    suffix = f", then {r.objective_after[0]}" if r.objective_after else ""
    return (
        f"{play.sides.numbers} {play.type} in {play.zone_relative}, "
        f"owner {play.owner.role}, {r.outcome} {r.enemy_deaths}-for-{r.ally_deaths}{suffix}"
    )
