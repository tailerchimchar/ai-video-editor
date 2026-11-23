# Spec: game understanding — phased plays in the digest (digest v3)

Status: approved by interview 2026-11-22 (owner present; red-team rulings accepted by the owner in one batch). Captured at commit `15d99d6` (v0.3.0).
Owner: Noodlz. Builder: the Claude session (media modules), the Grok bots after Fri 2026-11-27 (non-media tasks).
Example fixture: `docs/specs/examples/digest-v3-game-example.json` (built from the real Jhin 8/2/13 timeline, Riot facts only).

## 1. Problem

The digest describes a game as a flat list of clips with kills, deaths, assists, gold and a few transcript moments. Fight windows are cut by timestamp rules ("4 s before the first enemy death"), the story planner titles chapters from the gaming-expert's beat text ("Jhin kills MissFortune"), and the montage compiler's clip analyzer and refine loop shorten fights to 4-12 s. The owner's verdict on 2026-11-22: the story format works, but the editor does not know what happened in the game. It cannot say whether a fight was a 2v3 or a 5v5, whether the owner initiated or got caught, whether a pick led to a soul and a baron, or where the buildup started. Viewers want the buildup and the outcome, and the data has to be able to say so deterministically before any voice or title is written.

## 2. Goals

1. A per-game `game` section in the digest: three event-driven phases, and a list of plays with deterministic facts (type, phase, sides and numbers, zone, owner role, result, objectives after) and each play's own approach, engage and outcome window (D1-D6, D12).
2. Fight clips that play from the approach (story, shorts) or the engage (montage) to the outcome, never cut by a timer, with the window authoritative over every downstream trimmer (D2, D3, D8, R2, R9).
3. A story planner that cuts chapters one-to-one from plays with seamless transitions and no text cards, and a montage that jump-cuts dead time on the beat and ranks fights by an impressive factor (D7, D8, R2, R9).
4. A play vocabulary the owner can teach: one hand-labelled game as the precision gate, corrections through the mentor tool, a YAML per game read by the VLM prompts and the planner (D11, R6, R8).
5. Cost inside $1.50 per game for the understanding pass, chosen by a quality test (D10).
6. Digest v3 stays readable by the bots and Jev; old digests stay valid (D12, D13).

## 3. Non-goals (this spec)

- Play types beyond the eight in v1: rotations, macro, 1v1, soul and baron flips, bot-rotates-top are v2 (D4).
- The impressive-factor scoring formula; v1 defines the fields and a stable rank, the scale is the owner's later decision (D6).
- Calibrating the HP-bar reader or adding the enemy bar; trade proposals use it only when `hp_reader_enabled` is on, otherwise trades come from the VLM sweep (D1, prior spec R4).
- Recorded narration (experiment 2 of the content-direction spec) and the mic level problem (hardware; owner is adjusting Windows and Outplayed gain).
- TTS, upload code, team-gold comeback arcs (unchanged from spec-content-direction).

## 4. Design

### 4.1 Phases (`league/phases.py`)

- `early` ends at the first tower kill or 14:00, whichever comes first, but never before 8:00 (R10). `late` begins at the first baron kill or inhibitor kill, else 25:00. `mid` is between. Each boundary records `ended_by` / `started_by` (`first_tower`, `14:00`, `first_baron`, `first_inhibitor`, `25:00`).
- A game that ends before `late` would begin has only `early` and `mid`; a `closeout` play belongs to whichever phase holds the game end. A remake (< 5 min, no kills) has no phases and no plays.
- `taxonomy.Phase` and the story lane chapter read `game.phases.early.end_s` instead of the 14:00 and 15/25 constants (R10).

### 4.2 Play proposer (`league/plays.py`, Riot only, free)

- Reads what `parse_timeline` ignores today: `position` on every `CHAMPION_KILL`, `participantFrames[*].position`, `TURRET_PLATE_DESTROYED`, `DRAGON_SOUL_GIVEN`, `CHAMPION_SPECIAL_KILL`, `ELITE_MONSTER_KILL` with `killerTeamId`, `BUILDING_KILL` with `buildingType` and `laneType`.
- Fights are kills chained within 15 s (unchanged). Each fight becomes a play with:
  - `type` from the proposal rule of the YAML (4.4): `teamfight` (>= 3 enemy deaths and >= 6 champions touched a kill), `objective_fight` (an elite kill within 30 s after, or the first kill inside the dragon or baron pit), `gank` (early phase, one kill, three champions, in a lane), `dive` (a kill within 800 units of a standing enemy or ally tower), `caught_out` (one ally death, no enemy death, victim alone by assisters, not in lane), `closeout` (>= 3 enemy deaths within 60 s of game end), `skirmish` (everything else), `trade` (no kill: HP reader when enabled, else the VLM sweep).
  - `sides`: allies and enemies who touched a kill (killer, victim, assisters), as champion names only (prior spec R9); `numbers` as a **lower bound** string `"5v4"` with `numbers_source: riot_lower_bound` until the VLM confirms (R1).
  - `zone`: coarse named zone from the first kill position (`mid_lane`, `bot_lane`, `top_lane`, `dragon_pit`, `baron_pit`, `bot_river`, `top_river`, `*_jungle`, `*_base`), plus `zone_relative` (`ally_jungle`, `enemy_jungle`, ...) computed from the owner's side.
  - `owner`: `involved`, `role` (`initiator_kill`, `follow_up`, `caught_first`, `absent`), `kills`, `assists`, `died`.
  - `result`: enemy and ally deaths, `outcome` (`won`, `lost`, `even`), `objective_after` and `building_after` within 30 s.
  - `flags`: `help_from_team` (>= 2 allies touched a kill), `soul_point`, `steal` (elite killed by the team that did not have the most champions nearby at the previous frame), `flank: null` until the VLM fills it.
  - `game_time` and `recording_time` for first and last kill; `window` left `null` with `source: vlm_pending`.
  - `impressive`: `numbers_faced` (confirmed enemies minus confirmed allies other than the owner; null until confirmed), `score: null`.
- Plays with `owner.involved == false` are kept and tagged `absent`; they never feed shorts and feed the story only as context for a neighbouring owner play.

### 4.3 VLM play confirmer (`vlm/play_confirm.py`, League prompt, Haiku vision)

- For every proposed play, watches the recording at 1 fps from `first_kill - 30 s` to `last_kill + 20 s` (2x2 panels, ~50 panels, about $0.05 per play) and returns: `approach_s` (both teams visible and positioning), `engage_s` (first ability or poke), `outcome_s` (objective taken, tower falls, teams back off, banner done), confirmed `numbers` by health-bar count at the engage, `flank` (bool), and `banner_s` (the first kill banner seen).
- **Re-anchor** (R5): `banner_s - recording_time.first_kill_s` is stored as `offset_correction_s`; a play whose banner is not found within ±5 s is tagged `offset_uncertain` and kept out of shorts and montage.
- **Approach not found** (R3): when the owner is dead, off-screen or arriving late, `approach_s = first_kill - 8 s`, tagged `approach_unconfirmed`; such plays count for the story, never for a short.
- A sweep at 0.25 fps over the whole game proposes kill-less plays (`trade`, `caught_out` without a death) only when the budget allows after the confirmations (D1, D10).
- Cost: hard stop at `game_understanding_budget_usd` (default 1.50) under its own ledger key `plays:<asset>` (D15); the fps for the sweep and the confirmer is chosen once by the quality test (4.6) and stored in config.
- Caching (R6): results live in `WORKSPACE/plays/<asset>.json` keyed by the YAML hash; a YAML edit or a new correction never re-runs an analysed game. Re-run is on demand per asset (`--force`).

### 4.4 Play vocabulary (`vlm/game_hints/league_plays.yaml`)

One entry per play type: `phase`, `propose` (the Riot rule in words and in a small expression the proposer evaluates), `start` (`story`, `montage`, `fallback`), `end` (rule and a maximum), `vlm_confirms`, `facts_from_riot`, `examples` (appended from corrections). Loaded by the proposer, the confirmer prompt and the planner. Hash recorded in each play (`vocab_hash`).

### 4.5 Digest v3 (`grokbot/digest.py`)

- `Digest.version = 3`; new top-level `game_model: {phases, plays, objectives, vocab_hash, budget_spent_usd}` beside `clips` (`game` stays the game-name string the bots already read). Each `DigestClip` gains `play_id` (nullable). Readers of v2 digests treat `game` as absent.
- Play text in the digest is **templated from the facts only** (R4): `"{numbers} {type} in {zone_relative}, owner {role}, {outcome} {enemy_deaths}-for-{ally_deaths}{objective_suffix}"`. Free VLM prose stays in `WORKSPACE/plays/<asset>.json`. A play can never trip `digest_leaks`.
- The digest file is written pretty-printed: two-space indent, one key per line, keys in model order (D13). The same for `plays/<asset>.json` and `story.json`.
- Opponents stay champion names only; owner mic text never enters a play.

### 4.6 Labels and the precision gate (`league-plays-eval`)

- `WORKSPACE/labels/<asset>.json`: one row per play `{type, approach_s, engage_s, outcome_s, numbers, flank, note}` in recording seconds, written by the owner and the session together once (D11).
- A detected play matches a label when the windows overlap by >= 50% and the start is within 5 s (R8). The tool prints precision, recall and per-type confusion.
- Gate: plays drive the story and the montage once precision >= 0.8 on the labelled game. Until then the v2 planner and compiler remain the default (`plays_drive_cuts=false`).
- Quality test (D10): the same labelled game at 0.25 fps and 1 fps; the cheapest setting that passes the gate becomes the default.

### 4.7 Story planner v2 (`story/planner.py`)

- A chapter is exactly one play from approach to outcome; no 45 s fight cap, no merging (R2). Lane chapter and close chapter come from the `early` phase and the `closeout` play.
- Over `max_s` (360), whole plays are dropped in impressive-rank order (`absent` plays first, then lowest `numbers_faced`, then fewest owner kills), never trimmed.
- No text cards (D7): a 0.5 s dip or crossfade between chapters; a cash-register SFX (`sfx/kaching.wav`, royalty-free) at every owner kill, placed after any cut remapping (R9); the mic leg stays loudness-normalised over ducked game audio.
- The expert's beats are kept in the digest as `panel` data and are never a title source.

### 4.8 Montage v2 (`shorts.py` custom compiler, `music/arc.py`)

- Play windows are authoritative: when a clip carries a `play_id`, `_analyze_and_window` and `_apply_window_fixes` are skipped for it; the refine loop may drop a clip but never shorten it.
- Montage clips start at `engage_s - 3..5 s` and end at `outcome_s` (D2). Inside a play, gaps between kills longer than 3 s are jump-cut to the next beat (D8); only the final outcome cut snaps to the beat, inner cuts sit at Riot kill times (R9).
- Fit: fights ranked by impressive rank; the weakest are dropped until the music slot (60-90 s) fits. Never squeeze.
- Month pool (R7): at analysis time every montage-eligible play (`teamfight`, `objective_fight`, `closeout`, `skirmish` with >= 3 enemy deaths) is cut with 10 s padding to `WORKSPACE/month_pool/<YYYY-MM>/`; the cleanup guard never touches that folder; it is emptied after the montage ships.
- Cadence (D9): shorts 3-5 per week; a story only when the owner and the scorecard agree the game earns it; one montage every 4 weeks from the month pool.

### 4.9 Security and privacy

- No new secrets. VLM calls use the existing backend and keys in `api/.env` (never read by the session).
- Mic transcripts, VLM prose and labels stay local; the digest carries templated facts only (R4). Opponents by champion only.
- The month pool holds footage only; music provenance and the Drive guard are unchanged.

## 5. Testing

- Phases: synthetic timelines for first tower at 6:00 (early ends 8:00), no tower (14:00), surrender at 18:00 (no late), remake (no phases).
- Proposer: the example fixture reproduces 21 fights, 13 owner-involved, p15 = `teamfight 5v4 mid_lane initiator_kill won 4-1`, p19 = `objective_fight` with `objective_after = [DRAGON/AIR_DRAGON, BARON_NASHOR]`; `numbers_source == riot_lower_bound` everywhere before confirmation.
- Confirmer: mocked VLM returning approach/engage/outcome/banner; re-anchor stores the correction; missing banner → `offset_uncertain`; missing approach → fallback 8 s + `approach_unconfirmed`; budget stop at $1.50.
- Digest v3: v2 file loads with `game` absent; play text is templated and passes `digest_leaks` against a transcript that contains the same champion names; file is pretty-printed.
- Planner v2: one chapter per play, no merge of two plays 30 s apart, drop order by impressive rank, no cards in the render plan, kaching at remapped kill times.
- Montage v2: windows untouched by analyzer and refine; jump cuts at kill times; only the outcome cut moves on snap; month pool written and protected.
- Eval: labels fixture with 10 plays, matcher at 50% overlap / 5 s, precision report.

## 6. Decisions Log

| ID | Topic | Decision | Rationale | Source | Date |
|---|---|---|---|---|---|
| D1 | Detection | Riot first: positions, damage, wards, plates, elites, soul, buildings propose plays; VLM confirms and describes proposed windows plus a cheap low-fps sweep for kill-less plays | Cheapest, deterministic, explainable; most data is already on disk | Interview | 2026-11-22 |
| D2 | Buildup | Story and shorts start at the approach (both teams visible and positioning); montage starts at the engage, 3-5 s before the first ability or poke | "I like buildup, most people do"; montage is pace-driven | Interview | 2026-11-22 |
| D3 | Resolution | Default end is the outcome (objective, tower, teams back off, banner done); the end rule is per play type in the YAML | "it should depend" | Interview | 2026-11-22 |
| D4 | Taxonomy v1 | Eight plays Riot can prove: trade, gank, dive, skirmish, teamfight, objective fight, caught out, closeout; rotations, 1v1, macro, flips in v2 | Each type needs a detector, rules and labels | Interview | 2026-11-22 |
| D5 | Phases | Event-driven: early ends at first tower or 14:00; late begins at first baron or inhibitor, else 25:00 | A 22-min stomp and a 45-min game phase differently | Interview | 2026-11-22 |
| D6 | Story facts | Deterministic play facts (sides, numbers, flank, help from team, owner role, outcome, zone) are the story's source; the gaming-expert no longer titles anything; impressive-factor fields now, scoring later | "I'd rather the digest make it seem like it knows what it's talking about" | Interview | 2026-11-22 |
| D7 | On screen | No text cards ever; seamless transitions (Jawgemo, Zekken, Ryuk, Rival); kaching SFX on owner kills; the mic carries the story | Owner's reference editors | Interview | 2026-11-22 |
| D8 | Montage fit | Keep engage and outcome; jump-cut dead time > 3 s on the beat; rank by impressive factor; pool a month of games | How montages are actually cut; a bigger pool makes ranking selective | Interview | 2026-11-22 |
| D9 | Cadence | Shorts 3-5/week; story only when a game earns it (owner + scorecard); montage every 4 weeks | Owner's choice | Interview | 2026-11-22 |
| D10 | Budget | Up to $1.50 per game for the understanding pass inside the $2 cap; quality test (0.25 vs 1 fps) on the labelled game picks the default | "1-1.50 per game, test for quality" | Interview | 2026-11-22 |
| D11 | Teaching | One hand-labelled game is the precision test; later corrections via mentor_record_correction feed the YAML examples; YAML per game read by VLM and planner | Measurable before trusting the labels | Interview | 2026-11-22 |
| D12 | Migration | Digest v3: `game_model` section beside `clips` (key renamed from `game` at build time: `game` is already the name string), version field, clips point at plays; bots and Jev keep reading clips; old digests valid | Nothing breaks | Interview | 2026-11-22 |
| D13 | File format | digest.json, plays and story.json pretty-printed (2-space indent, one key per line, fixed key order) | "make sure that data is prettified" | Interview | 2026-11-22 |
| R1 | Numbers | Riot gives a lower bound; the VLM confirms the full count from health bars or the field stays null; impressive factor uses confirmed counts only | Assist lists miss body-blockers and late arrivals; frames are 1/min | Red Team | 2026-11-22 |
| R2 | Chapters | Plays are chapters 1:1; no 45 s cap; never merged; fit by dropping whole plays by impressive rank | The old cap would cut the approach | Red Team | 2026-11-22 |
| R3 | Approach missing | Fallback to first kill − 8 s, tagged approach_unconfirmed; story yes, shorts no | Owner dead or off-screen | Red Team | 2026-11-22 |
| R4 | Leak check | Play text templated from facts only; VLM prose local; a play can never fail the round | digest_leaks scans every string | Red Team | 2026-11-22 |
| R5 | Offset drift | VLM re-anchors each play on the kill banner, stores a per-play correction; no banner within ±5 s → offset_uncertain, no shorts or montage | Drift equals the engage lead | Red Team | 2026-11-22 |
| R6 | Cache staleness | Plays frozen per game with their YAML hash; re-run on demand per asset only | No library-wide re-spend on each correction | Red Team | 2026-11-22 |
| R7 | Month pool | Eligible plays cut with padding to a pinned month-pool folder at analysis time; cleanup never touches it; emptied after the montage ships | Outplayed and cleanup purge footage | Red Team | 2026-11-22 |
| R8 | Precision gate | labels.json per game; match at 50% overlap and start within 5 s; plays drive cuts at precision ≥ 0.8 | The fps and budget decisions need a measure | Red Team | 2026-11-22 |
| R9 | Snap and SFX | Only the outcome cut snaps to the beat; inner jump cuts fixed at Riot kill times; kaching placed after cut remapping | Snap must not move a kill frame | Red Team | 2026-11-22 |
| R10 | Phase edges | Early ends at max(first tower, 8:00) or 14:00; games ending before late have two phases; closeout lives in the phase holding game end; lane chapter reads phases | 6-minute towers and surrenders | Red Team | 2026-11-22 |
| D14 | Confirmer design | Per-frame state labels (none/approach/fighting/aftermath, kill banner, counts) on single 960-px frames with burned-in seconds at 0.5 fps; windows derived deterministically anchored on the Riot kill times; model `play_confirm_model` default claude-sonnet-4-6; owner plays confirmed first; sweep off by default | Real Jhin run: Haiku on mosaics with free-form seconds could not place events; Sonnet per-frame labels matched the footage | Drift Accepted | 2026-11-22 |
| D15 | Understanding budget key | The pass spends under `plays:<asset>`, outside the $2 asset cap (like `story:<asset>`); each run's cap is the spend already under that key plus `game_understanding_budget_usd`, so a forced rerun or the fps test gets its own budget; `SpendCapError` stops the pass and is reported (`budget_exhausted`) | The asset cap already holds other features' spend and the fps test runs twice | Drift Accepted | 2026-11-22 |
| D16 | Gate scope | Precision, the month pool and the montage consider owner-involved plays only; `offset_uncertain` plays never reach the montage | The owner cannot label off-screen fights; the fixture tops out at 0.62 otherwise | Drift Accepted | 2026-11-22 |
| D17 | Chapter overlap | A story chapter starts no earlier than the previous chapter's end; the banner offset correction shifts kill times for kaching and jump cuts | Windows up to 30 s before a kill can overlap; repeated footage reads as a defect | Drift Accepted | 2026-11-22 |

## 7. Dependency Graph & Implementation Order

```
T1 phases + play proposer from Riot (4.1, 4.2) + example fixture test                         independent
T2 play vocabulary YAML + loader, digest v3 model, templated text, pretty print, play_id (4.4, 4.5)   independent
T3 VLM play confirmer: windows, numbers, flank, banner re-anchor, budget, frozen cache (4.3)  depends on T1, T2
T4 labels.json + league-plays-eval matcher/precision + fps quality harness (4.6)              depends on T1, T2
T5 story planner v2: 1:1 chapters, drop by rank, no cards, transitions, kaching (4.7)          depends on T3
T6 montage v2: authoritative windows, jump cuts, outcome-only snap, ranking, month pool (4.8) depends on T3
T7 run: label the Jhin game with the owner, pass the gate, re-render Jhin story v3 and Nov montage v2   depends on T4, T5, T6
```

Order: T1, T2 in parallel → T3, T4 in parallel → T5, T6 in parallel → T7. T7 blocks on the owner's labelling session. Bots after Friday: T2 (model, YAML loader, pretty print) and T4 (eval tool) are non-media and bot-eligible; T3, T5, T6 are media code and stay with the session (prior spec R8).

## 8. Open items carried forward

- Impressive-factor scoring formula (owner's scale: 2v2 impressive, 2v3 and 1v2 super impressive, 1v1 super impressive).
- v2 play types: rotations, bot-rotates-top, 1v1, macro, soul and baron flips; needs denser positions (VLM minimap read) than Riot's 1/min frames.
- An owner role word for a fight the owner carried (3 of 4 kills); zone naming relative to the owner's side is in v1, a "carried" role is not.
- Experiment 2 (recorded narration) and the mic gain fix remain from spec-content-direction.
- Trade proposals via the HP reader wait for its calibration.
