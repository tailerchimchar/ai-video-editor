---
tags: [rule, story, early-game, series]
taught: 2026-11-26
status: superseded
superseded_by: "[[Early fight start]]"
supersedes: "[[Opening at 0-50 (superseded)]]"
---
# Early fight start: solo vs team (superseded)
Replaced on 2026-11-26 by [[Early fight start]]. The trigger is whether anything happens, not solo vs team.

- **[[Team games (ACL)]]:** a level-1 bush fight is real, so the opening
  starts at game **0:20** to show the approach and the whole fight.
- **[[Solo queue]]:** both laners standing in the bush is not a fight. The
  opening starts at about **1:00**, or at 0:20 only when a champion kill
  happens before 1:05. The owner may still move a game's start (Kai'sa: 0:32).

> Owner: "this one, we just chilled til 50 seconds ... different than the jhin team game because that one we actually fought at like 30 seconds"

**Code:** `league/early_game.py::_brawl_before_1_05(team_game=...)`; the
team-game check is Riot `gameType == CUSTOM_GAME`.
**Taught by:** [[Jhin 8-2-13 (2026-11-20)]] · [[Kai'sa 7-1-3 (2026-11-26)]] · [[Yunara 6-0-5 (2026-11-25)]]
