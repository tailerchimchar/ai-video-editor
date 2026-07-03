# League of Legends game hints

## HUD layout (1920x1080 reference)

- **Scoreline**: top-right, small text — team kills / personal KDA / clock
- **Champion portrait + summoner spells**: bottom-left corner
- **Killfeed**: top-right, below the scoreline — text ribbons showing
  who killed whom with champion icons
- **Item bar (6 slots + trinket)**: bottom-center
- **Minimap**: bottom-right square
- **Ability bar**: bottom-center under the item bar (Q/W/E/R)

## Event lexicon

Kills:
- **First blood** — the first champion kill of the game
- **Double / triple / quadra / penta kill** — 2/3/4/5 kills within
  a short time window by the same champion
- **Ace** — the entire enemy team dies within a short window
- **Shutdown** — a killstreak champion is killed

Objectives:
- **Baron / Baron Nashor** — the big buff monster in the top river
- **Dragon** — smaller buff monster in the bottom river (Cloud /
  Infernal / Ocean / Mountain / Hextech / Chemtech + Elder)
- **Herald** — early-game objective that spawns a battering ram
- **Turret / inhibitor** — structures you destroy

Team fights: 3+ champions from each team engaging together.

## Common false positives (things that LOOK like events but aren't)

- **Loud teamfight audio** without a kill on screen — often just chip
  damage or trading. Not a highlight.
- **Champion using a big ultimate ability** — visually flashy but if
  it whiffs (no takedown, no save) it's not a highlight.
- **Recall / base animation** — the champion glows and floats;
  sometimes misread as an ultimate cast.
- **Death cam replay** — right after a champion dies the camera
  sometimes replays their death for a couple seconds; if this shows
  in the sampled frames the "kill" already happened before the clip's
  claimed timestamp.

## What "good context" looks like for a kill highlight

- 1-3 seconds before the kill showing positioning + engage
- The actual kill moment (skillshot hit / autos / ult connecting)
- 1-2 seconds after showing the killfeed confirmation + the reward
  animation ("+X gold", killstreak announcement)

Cuts that start AT the kill moment feel abrupt — no setup. Cuts that
end well before the killfeed confirms feel unfinished.
