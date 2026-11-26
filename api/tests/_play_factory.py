"""Tiny Play builder shared by the montage tests."""

from __future__ import annotations

from ai_video_editor.league.play_models import Play


def make_play(
    pid: str = "p1",
    *,
    type: str = "teamfight",
    engage: float | None = 100.0,
    outcome: float | None = 120.0,
    approach: float | None = 95.0,
    first_kill: float = 105.0,
    last_kill: float = 118.0,
    enemy_deaths: int = 3,
    flags: list[str] | None = None,
    numbers_faced: int | None = 4,
    kills: int = 2,
    phase: str = "mid",
    involved: bool = True,
    rank: int | None = None,
    text: str = "",
    offset_correction: float | None = None,
    rec_minus_game: float = 0.0,
    outcome_result: str = "won",
) -> Play:
    return Play.model_validate(
        {
            "id": pid,
            "type": type,
            "phase": phase,
            "game_time": {"first_kill_s": first_kill, "last_kill_s": last_kill},
            "recording_time": {
                "first_kill_s": first_kill + rec_minus_game,
                "last_kill_s": last_kill + rec_minus_game,
            },
            "window": {
                "approach_s": approach,
                "engage_s": engage,
                "outcome_s": outcome,
                "flags": flags or [],
                "offset_correction_s": offset_correction,
            },
            "zone": "mid",
            "sides": {"allies": ["A"], "enemies": ["B"], "numbers": "5v4"},
            "owner": {
                "involved": involved,
                "role": "initiator_kill",
                "kills": kills,
                "assists": 0,
                "died": False,
            },
            "result": {"enemy_deaths": enemy_deaths, "ally_deaths": 1, "outcome": outcome_result},
            "flags": {"help_from_team": True},
            "impressive": {"numbers_faced": numbers_faced, "rank": rank},
            "text": text,
        }
    )
