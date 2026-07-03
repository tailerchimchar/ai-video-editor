# Valorant game hints

## HUD layout (1920x1080 reference)

- **Round score**: top-center — attacker score / defender score /
  round timer / current round number
- **Killfeed**: top-right — text ribbons with agent portraits, weapon
  icon, victim name, and headshot marker when applicable
- **Loadout + credits**: bottom (agent icon, weapon, shield, credits)
- **Ability slots**: bottom-center (C / Q / E / X)
- **Minimap**: top-left corner
- **Ping / callout wheel**: appears mid-screen briefly when active

## Event lexicon

Kills:
- **Ace** — one player kills all 5 enemies in a single round
- **Clutch** — winning a round while outnumbered (1v2 / 1v3 / 1v4 / 1v5)
- **Entry frag** — the first kill of a round, usually the entry player
- **Headshot** — a kill via headshot (small marker in the killfeed)
- **Knife kill** — a kill with the melee weapon (rare, very hype)
- **Wallbang** — a kill through a wall (marker in the killfeed)

Utility:
- **Smoke** — Brimstone/Omen/Astra/Viper/Harbor obscure vision
- **Molly / incendiary** — area denial fire (KAY/O, Phoenix, Brim)
- **Flash** — pop-flash that blinds enemies (Reyna, Skye, Yoru, Phoenix, KAY/O)
- **Ult** — agent ultimate (usually distinctive per agent)

Rounds: 2 halves x 12 rounds each. Overtime possible.

## Common false positives (things that LOOK like events but aren't)

- **Killfeed showing a teammate's kill** — top-right shows a kill but
  the person recording didn't get it. Highlight belongs to a teammate,
  not the player of interest.
- **Retake / clutch attempt that FAILS** — a 1v3 attempt is only a
  highlight if the player wins; a dramatic failure is not usually a
  reel-worthy moment.
- **Loud comms / teammate laughter** — audio_peak sometimes fires on
  vocal reactions; if the frames show a downtime moment (buy phase,
  agent select) there's no visual event.
- **Watching a spectator killfeed post-death** — the player died,
  is spectating, and the killfeed keeps updating. The player of
  interest isn't in these frames.

## What "good context" looks like for a kill highlight

- 1-3 seconds before the kill showing peek / position / setup
- The actual kill moment (crosshair on target, shot landing,
  killfeed pops)
- 1-2 seconds after showing the killfeed confirmation and any
  follow-up (multi-kill sequence, callout, celebration)

Cuts that start AT the shot feel abrupt. Cuts that end before the
killfeed confirms feel unfinished. Multi-kill sequences should
capture the full run of kills, not just the first.
