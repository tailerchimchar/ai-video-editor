---
tags: [rule, story, fights]
taught: 2026-11-26
status: active
---
# Kills you assist keep their payoff
A fight clip never ends on the frame an enemy dies. Your kills, your deaths **and kills you assist on** keep 2 s of footage after them (`EVENT_POST_S`).

Riot sometimes credits the last hit to a teammate even when it looks like your kill on screen. The kill still has to land on camera.

**Taught by:** [[Yunara 6-0-5 (2026-11-25)]]: the Cho'Gath kill (Hwei last hit, owner assist) near the end was cut off.
**Code:** `story/planner.py::_play_chapter`.
