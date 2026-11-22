# Spec: the Jhin round — Riot timeline, event-driven windows, digest v2, Jev audit

Status: approved by interview 2026-11-22 (user asleep for the build; rulings recorded). Captured at commit `def8b37`.
Owner: Noodlz. Builder: the Claude session on the PC (the Grok bots are frozen until Fri 2026-11-27).

## 1. Problem

The Zeri round (2026-11-19) produced three shorts the owner rejected on layout (bare centre crop), caption placement (over the ability bar), and timing (fixed 7.5 s pads cut off build-up and the game-ending payoff). Two of seven kept clips contained no on-screen event at all. The digest the bots read had an empty voice field, no per-clip events and no game state, and one clip was named after an objective ("Baron") that never happened. Jev's answers were unrecorded.

Root causes, from pre-analysis: the Riot timeline is fetched and discarded; windows are `anchor ± pad`; the digest reads only panel JSON with a schema mismatch; Jev logs nothing; the HUD layout exists but is off and unreachable from the shorts compiler.

## 2. Goals

1. Run the Jhin 8/2/13 game (recorded 2026-11-20, asset `42672a10-5b17-4f60-9231-933b3c90ab97`) end to end and produce 2 to 3 vertical shorts in layout B (`9:16_hud`) that the owner would post.
2. Clip windows come from events, not constants.
3. The digest the Editor bot reads carries events, game state and a voice paraphrase.
4. Every Jev answer is logged and comparable with the owner's verdicts.
5. Cost per game is capped and visible.

## 3. Non-goals (this round)

- Per-minute frames for other players; team gold swings (next round).
- Teaching the shorts compiler layout B (deferred; per-edit path accepted).
- 1080x1920 output; fewer re-encodes.
- The arc planner / story video.
- Any change to what the bots do; they are frozen. Outputs land in Drive for them.
- Fixing the blocked bot PRs (#13, #14, #16, #17, #19, #21).

## 4. Design

### 4.1 Riot timeline cache and parser (`league/riot_timeline.py`, `league/candidates/riot.py`)

- After correlation, save `match.json` and `timeline.json` under `WORKSPACE/riot/<asset_id>/` together with `meta.json` (matchId, confidence, offset used). Later runs for the same asset read the cache and skip the 20-match lookback (R6).
- Parse all timeline events into a typed list: `CHAMPION_KILL` (killer, victim, assisters), deaths of allies and enemies, `BUILDING_KILL` (towers, inhibitors, nexus towers), `ELITE_MONSTER_KILL`, and game end from `GAME_END` or `gameEndTimestamp`. Actors are rendered as champion + side: `you (Jhin)`, `ally Thresh`, `enemy Diana` (D5).
- Parse the owner's own `participantFrames` per minute: gold, CS, level (D2). No other players' frames.
- Game clock → recording seconds through the existing offset pipeline. Add a piecewise check: run `align_offset` on the first and second half of the recording; if the shifts differ by more than 3 s, log a warning and widen the pre-pad to 8 s for that recording (R3).
- Correlation confidence gates everything: `low` → no event windows, no game state, fall back to the anchor strategy; `medium` → event windows allowed, digest stamped `match_confidence: medium`; `high` → full (R4). The cache is keyed by asset id and records the confidence; a low pick is never reused.
- Rate limits: honour `Retry-After` once on 429, then fail the source softly (R6). Personal key: 20 req/s, 100 per 2 min.

### 4.2 Event-driven windows (`highlights.py`)

- Candidates from the Riot source carry `metadata.fight_start_s` and `metadata.fight_end_s`, computed at candidate time: chain any kill, death or assist within 15 s of the previous into one fight (D3).
- `_window_anchor` branches on those fields when present (R1): window = `fight_start_s − 4` to `fight_end_s + 3`. If a nexus kill or game end falls within 25 s after `fight_end_s`, extend to it. The ranker's `suggested_start/end` may only widen. Cluster midpoint anchors are no longer used when fight bounds exist.
- Cap stays 120 s. When a window exceeds it, trim from the front so the payoff is kept (R2).
- With no fight bounds (Outplayed-only candidates, low confidence), behaviour is byte-for-byte today's.

### 4.3 Digest v2 (`grokbot/digest.py`)

`DigestClip` gains:
- `events: list[{type, t_in_clip, actors}]` from the parsed timeline, clipped to the window.
- `game_state: {game_time_s, you: {gold, cs, level} at window start and end, game_ended_in_clip: bool, match_confidence}`.
- `voice: str` fixed: one sentence written by Haiku from the clip's transcript slice (D4) with the instruction "one sentence, what the player says or reacts to, no quotes, no slurs, no names"; `energy: str` from loudness and sentiment (hype_rising, laughter, panic, calm). The gaming-expert `story_beats` dict-vs-str bug is fixed by reading `beat` text.
- The leak check runs with `min_len=8` for the voice field (R8). If the paraphrase reads badly, record why in the mentor store and revisit (D4).
- Clip names and copy never name an objective or location unless an event confirms it (mentor rule 2026-11-22).

### 4.4 Jev audit (`decisions/audit.py`, `jev-scorecard`)

- Every `ask_many` call appends one JSONL line per answer to `WORKSPACE/decisions/jev-<date>.jsonl`: timestamp, backend, question id, state hash and a compact state excerpt, answer, probability. Local only, never uploaded (D8).
- `uv run jev-scorecard --game league` joins the log with the mentor corrections and prints: keep precision against "shouldn't have been kept" verdicts, format agreement, and the clips where they disagree.
- The mentor corrections file moves out of the package: `GAME_HINTS_DIR` (default `WORKSPACE/game_hints`), with a one-time migration of the file currently sitting untracked in the runner checkout (R7).

### 4.5 Rendering (`shorts.py`, `edits.py`)

- Shorts for this round go through the per-edit `9:16_hud` caption path with `HUD_ASPECT_ENABLED=true` (D7).
- `shorts.py` reads the encoder preset and CRF from config instead of the hardcoded `veryfast`/`20`.
- One short is rendered twice, default and `slow`/CRF 18, and both go to Drive `renders/compare/` for the owner.

### 4.6 Cost (`anthropic_surface.py`)

- A per-asset dollar ledger in SQLite, filled from `response.usage` on every Anthropic call (ranker, paraphrase, VLM), checked before each call; over `$2.00` per asset the call is refused and the step degrades (R5). Jev is logged but not counted (near-free). Ranker stays on Sonnet 4.6 (D9).

### 4.7 Security

- Keys live only in `api/.env`. A pre-publish secret scan (`uv run secret-scan`) greps the merged diff, the Jev log, the Riot cache, `digest.json` and every file about to be uploaded for key shapes (Anthropic, TypeSafe, Riot, Google OAuth, GitHub, Langfuse) and refuses on a hit (R9). The scan also runs before each merge in this round.
- IGNs are not private per the owner (D5); teammate speech is paraphrased, never quoted (R8).

## 5. Testing

- Pure tests with invented timelines for the parser, the chain rule, cap trimming, the confidence gate and the cache.
- Window tests assert the Outplayed-only path is unchanged.
- Digest tests cover the new fields, the voice fallback when Haiku is unavailable, and the leak check at `min_len=8`.
- Scorecard test on an invented log plus invented corrections.
- Ledger test: refuses the call that would cross the cap.
- One real-ffmpeg test already exists for the HUD caption path; reuse it for the encoder-config change.

## 6. Decisions Log

| ID | Topic | Decision | Rationale | Source | Date |
|---|---|---|---|---|---|
| D1 | Game and output | Jhin 8/2/13; 2-3 shorts in layout B | Most events of the three games; best test of windows | Interview | 2026-11-22 |
| D2 | Timeline scope | All events + the owner's own per-minute frames | Enough for windows, honest naming and "how was my lane"; no team swing yet | Interview | 2026-11-22 |
| D3 | Window rule | Chain within 15 s; pads 4/3; extend ≤25 s to nexus or game end; ranker widens only; cap 120 | Replaces fixed pads with evidence; owner wanted build-up and payoff | Interview | 2026-11-22 |
| D4 | Voice field | Haiku paraphrase per kept clip; if bad, record why and revisit | Ranker already sends transcript text to Anthropic; bots and Drive are the boundary | Interview | 2026-11-22 |
| D5 | Identity | Champion + side in events; IGN rule dropped | Owner: IGNs are not PII; champion+side reads better anyway | Interview | 2026-11-22 |
| D6 | Jev audit | JSONL log per answer + scorecard against verdicts | Answers were invisible; owner validated them by hand | Interview | 2026-11-22 |
| D7 | Render path | Per-edit 9:16_hud now, compiler later; fix shorts encoder config; test slow/CRF 18 | Layout B chosen; compiler change is a render-path PR for next week | Interview | 2026-11-22 |
| D8 | Process | Jira split; Claude implements and merges all; speed over ceremony; hard line = no leaked keys | Owner's words; bots frozen | Interview | 2026-11-22 |
| D9 | Cost | Sonnet ranker; cap $2 per game | Judge the new windows with the better ranker first | Interview | 2026-11-22 |
| R1 | Window routing | Fight bounds in candidate metadata; `_window_anchor` branches on them | A `_STRATEGIES` entry would be dead code: `cut_window` short-circuits on `anchor_seconds` | Red Team | 2026-11-22 |
| R2 | Cap direction | Trim from the front when over 120 s | Keeps the payoff the extension was for | Red Team | 2026-11-22 |
| R3 | Offset drift | Piecewise `align_offset` check; warn and widen pre-pad to 8 s on >3 s disagreement | One global shift cannot absorb a pause; 4 s pad is less forgiving than 7.5 | Red Team | 2026-11-22 |
| R4 | Wrong match | Confidence gate: low → anchor strategy and no game state; medium → stamped; cache keyed by asset id with confidence | A wrong match cached by matchId would become ground truth | Red Team | 2026-11-22 |
| R5 | Cost cap enforcement | Per-asset dollar ledger in SQLite from `response.usage`, checked before each paid call | `reserve_call` counts calls per process, not dollars per game | Red Team | 2026-11-22 |
| R6 | Rate limits | Cache asset→match mapping; honour Retry-After once | 23 calls per run with no backoff | Red Team | 2026-11-22 |
| R7 | Corrections file | `GAME_HINTS_DIR` default `WORKSPACE/game_hints`; migrate the untracked file | Data, not code; today it lives only in the runner checkout | Red Team | 2026-11-22 |
| R8 | Voice leakage | Haiku prompt forbids quotes, slurs, names; leak check `min_len=8` on voice | Verbatim guard alone misses short quotes | Red Team | 2026-11-22 |
| R9 | Secret scan | `secret-scan` before merge and before publish | Single-session merging has no second pair of eyes on the combined result | Red Team | 2026-11-22 |

## 7. Dependency Graph & Implementation Order

```
T1 riot timeline cache + parser + confidence gate + Retry-After   (league/riot_timeline.py, riot.py)
T2 event-driven windows (fight bounds in metadata; _window_anchor branch; cap trim)   depends on T1
T3 digest v2 (events, game_state, voice via Haiku, energy, leak min_len)   depends on T1
T4 jev audit log + scorecard + GAME_HINTS_DIR migration   independent
T5 cost ledger + shorts encoder config + secret-scan   independent
T6 run the Jhin round: candidates → rank → highlights → panel → publish → 2-3 shorts in layout B → Drive   depends on T1-T5
```

Order: T1 → (T2, T3, T4, T5 in parallel worktrees) → T6.

## 8. Open items carried forward

- Shorts compiler layout B; 1080x1920 output; fewer re-encodes.
- Team-level game state and the arc planner.
- Whether the Haiku paraphrase is good enough (D4 revisit).
- Bot PRs #13/#14/#16/#17/#19/#21 after the freeze.
