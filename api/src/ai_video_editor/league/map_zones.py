"""Summoner's Rift geometry: coarse named zones and tower proximity.

Pure functions over game coordinates (map ~14820 x 14881, blue base at the
bottom-left). No dependencies."""

from __future__ import annotations

import math

TOWERS: dict[int, list[tuple[int, int]]] = {
    100: [
        (10504, 1029), (6919, 1483), (4281, 1253), (5846, 6396), (5048, 4812), (3651, 3696),
        (981, 10441), (1512, 6699), (1169, 4287), (1748, 2270), (2177, 1807),
    ],
    200: [
        (13866, 4505), (13327, 8226), (13624, 10572), (8955, 8510), (9767, 10113), (11134, 11207),
        (4318, 13875), (7943, 13411), (10481, 13650), (12611, 13084), (13052, 12612),
    ],
}  # fmt: skip

_DRAGON_PIT = (9866, 4414)
_BARON_PIT = (5007, 10471)
_PIT_RADIUS = 1500
_RIVER_DIAGONAL = 14850
UNKNOWN = "unknown"


def zone_of(x: float | None, y: float | None) -> str:
    if x is None or y is None:
        return UNKNOWN
    if x + y < 4500:
        return "blue_base"
    if x + y > 25000:
        return "red_base"
    if math.hypot(x - _DRAGON_PIT[0], y - _DRAGON_PIT[1]) <= _PIT_RADIUS:
        return "dragon_pit"
    if math.hypot(x - _BARON_PIT[0], y - _BARON_PIT[1]) <= _PIT_RADIUS:
        return "baron_pit"
    if abs(x - y) < 1800:
        return "mid_lane"
    if y < 2600 or (x > 12300 and y < 7000):
        return "bot_lane"
    if x < 2600 or y > 12300:
        return "top_lane"
    if abs(x + y - _RIVER_DIAGONAL) < 1800:
        return "bot_river" if x > y else "top_river"
    side = "blue" if x + y < _RIVER_DIAGONAL else "red"
    return f"{side}_{'bot' if x > y else 'top'}_jungle"


def zone_relative(zone: str, owner_team_id: int | None) -> str:
    """`blue_*` / `red_*` zones relative to the owner's side (100 = blue):
    `ally_base`, `enemy_jungle`. Lanes, pits and rivers pass through."""
    if owner_team_id is None:
        return zone
    for colour, team in (("blue", 100), ("red", 200)):
        if zone.startswith(colour + "_"):
            side = "ally" if owner_team_id == team else "enemy"
            return f"{side}_jungle" if zone.endswith("_jungle") else f"{side}_base"
    return zone


def near_tower(x: float | None, y: float | None, radius: float = 800.0) -> int | None:
    """Team id of the nearest tower within `radius`, else None."""
    if x is None or y is None:
        return None
    best: tuple[float, int] | None = None
    for team, towers in TOWERS.items():
        for tx, ty in towers:
            d = math.hypot(x - tx, y - ty)
            if d <= radius and (best is None or d < best[0]):
                best = (d, team)
    return best[1] if best else None
