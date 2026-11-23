# Game understanding: plays, the gate, story v2 and montage v2

Shipped in v0.4.0 (spec `docs/specs/spec-game-understanding.md`, plan `docs/plans/2026-11-22-game-understanding.md`, Jira AVE-45).

## What a game becomes

`league-understand --asset <id>` reads the cached Riot timeline (`WORKSPACE/riot/<asset>/`), proposes plays from kills, positions and objectives (free), then confirms each owner-involved play with the vision model (default `claude-sonnet-4-6`, single 960-px frames every 2 s with the second burned in, per-frame state labels; the window is derived from the labels anchored on the Riot kill times). Output, pretty JSON:

- `WORKSPACE/plays/<asset>.json`: `GameModel` with `phases`, ranked `plays` (type, phase, zone, sides as a Riot lower bound plus VLM-confirmed counts, owner role, result, objectives after, `window.approach_s/engage_s/outcome_s`, flags such as `approach_unconfirmed`, `offset_uncertain`, `budget_exhausted` on the model), `objectives`, `vocab_hash`, `budget_spent_usd`.
- `WORKSPACE/plays/<asset>.prose.json`: the model's one-line descriptions (never published).
- `WORKSPACE/month_pool/<YYYY-MM>/`: montage-eligible owner plays cut with 10 s padding, plus `index.json` (unless `--no-pool`).

Frozen: a second run returns the file unchanged (a vocabulary change only logs a warning); `--force` re-runs and re-spends. Spend is ledgered under `plays:<asset>` with `GAME_UNDERSTANDING_BUDGET_USD` (default 1.50) per run, outside the $2 asset cap.

## The gate

1. Label one game with the owner: `WORKSPACE/labels/<asset>.json` = `{"asset_id": ..., "rows": [{"type", "approach_s", "engage_s", "outcome_s", "numbers", "flank", "note"}]}` in recording seconds.
2. `league-plays-eval --asset <id>` prints precision, recall, per-type figures and type confusions over owner-involved plays; `--fps 0.25,0.5,1.0 --source <mp4>` re-runs the pass per setting and prints cost and precision for each.
3. The gate is precision ≥ 0.8. When it passes, append `PLAYS_DRIVE_CUTS=true` (and the chosen `PLAY_CONFIRM_FPS`) to `api/.env`; the eval CLI prints the exact line.

## Consumers

- Story: `story-render --asset <id> --round <folder> [--plays | --no-plays] [--review]`. With `PLAYS_DRIVE_CUTS=true` and a frozen file present the plays path is the default: one chapter per owner play (approach to outcome, never capped or merged, no overlap, whole plays dropped by rank over 360 s), no text cards, 0.25 s dips, kaching at every owner kill.
- Montage: `POST /api/v1/shorts/custom_montage` with `play_specs` (from `montage.plan.fit_plays` → `segment_to_spec`): locked windows bypass the analyzer and the refine loop, gaps over 3 s between kills are jump-cut, only the outcome cut snaps to the beat, kaching after remapping.
- Digest v3: `grokbot-publish-round --asset-id <id> ...` loads the frozen file and publishes `game_model` beside `clips` with `play_id` on each clip; play text is templated facts only.

## Settings

`GAME_UNDERSTANDING_BUDGET_USD` 1.5 · `PLAYS_DRIVE_CUTS` false · `PLAY_CONFIRM_MODEL` claude-sonnet-4-6 · `PLAY_CONFIRM_FPS` 0.5 · `PLAY_SWEEP_ENABLED` false · `PLAY_SWEEP_FPS` 0.25 · `STORY_TRANSITION_S` 0.25 · `STORY_KILL_SFX` true · `MONTAGE_JUMP_CUT_GAP_S` 3.0. The kill chime is `WORKSPACE/sfx/kaching.wav` (synthesised on first use; drop a real one there to replace it). The play vocabulary is `vlm/game_hints/league_plays.yaml`, overridable at `WORKSPACE/game_hints/league_plays.yaml`.
