# Default game hints

Fallback when the asset's game isn't recognized (no specific hints
file). Enough generic guidance that the loop works out of the box on
a new game; add a `<game>.md` file next to this one to specialize.

## HUD layout

Games vary — the killfeed / scoreboard / minimap is usually somewhere
along the top or bottom edge of the frame. Look for text ribbons or
icons that indicate the event happened.

## Event lexicon (generic)

Kills:
- **First kill / opener** — the first significant kill of the round
  or life
- **Multi-kill** — the same player scoring multiple kills in quick
  succession
- **Clutch** — winning a fight while at a disadvantage

Interesting moments (game-agnostic):
- **Unusual play** — flanking, unexpected engage, ability combo
- **Team fight / team wipe** — multiple players engaging together
- **Objective take** — capturing / destroying / neutralizing a
  game-specific goal

## What "good context" looks like

- 1-3 seconds of setup before the payoff (positioning, aim, engage)
- The payoff moment itself (the shot / hit / capture)
- 1-2 seconds after showing the confirmation (score change, killfeed,
  reward)

## Common false positives

- **Loud audio without a visible event** — cheers / callouts /
  reactions that don't correspond to what's on screen
- **Highlight of a different player** — the killfeed shows a
  teammate's action; the person recording isn't the one who did it
- **Failed attempt** — a dramatic move that didn't work (the enemy
  won the fight, the objective was contested away)
