# Game Understanding (digest v3 phased plays) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every League game a `game` section in the digest: event-driven phases and a list of plays with deterministic Riot facts and VLM-confirmed approach/engage/outcome windows, and make the story planner and the montage compiler cut from those windows instead of timers.

**Architecture:** A pure Riot proposer (`league/plays.py`) turns the cached timeline into typed `Play` records (positions, participants, objectives); a VLM confirmer (`vlm/play_confirm.py`) watches each proposed window once, re-anchors it on the kill banner and fills the window; results are frozen per asset in `WORKSPACE/plays/<asset>.json` keyed by the vocabulary hash. Digest v3 carries the `GameModel` beside `clips` with templated text only. The story planner and the custom montage compiler read plays; their windows are authoritative over the clip analyzer and the refine loop. A labels file and an eval CLI gate the switch (`plays_drive_cuts`).

**Tech Stack:** Python 3.10+, pydantic v2, ffmpeg, Anthropic Haiku vision through the existing `vlm/backends`, pyyaml (new runtime dependency), pytest.

**Spec:** `docs/specs/spec-game-understanding.md` (D1-D13, R1-R10). Example fixture: `docs/specs/examples/digest-v3-game-example.json`.

## Global Constraints

- Tests: `uv run pytest -q` from `api/`; lint `uv run ruff check src/ && uv run ruff format src/`; `secret-scan` before merge. Worktree testing: main venv python with `PYTHONPATH=<worktree>/api/src` and temp `WORKSPACE_DIR` / `OUTPLAYED_MEDIA_DIR` (set in the dispatch).
- Never read `api/.env`. No new secrets. Opponents are champion names only. Owner mic text never enters a play. VLM prose never enters the digest; only the template `"{numbers} {type} in {zone_relative}, owner {role}, {outcome} {enemy_deaths}-for-{ally_deaths}{objective_suffix}"` (R4).
- Pretty JSON everywhere this plan writes: `model_dump_json(indent=2)` / `json.dumps(..., indent=2)` (D13).
- Phases: early ends at `max(first_tower_s, 480)` or 840, whichever first; late begins at first baron or inhibitor kill, else 1500; games ending before late have two phases; closeout belongs to the phase holding game end (D5, R10).
- Windows: story and shorts start at `approach_s`, montage at `engage_s - 3.0`; end at `outcome_s`; fallback approach `first_kill_s - 8.0` tagged `approach_unconfirmed` (story only, never shorts); banner not within ±5 s → `offset_uncertain` (no shorts, no montage) (D2, D3, R3, R5).
- Numbers: Riot gives a lower bound (`numbers_source="riot_lower_bound"`); the VLM confirms (`"vlm_confirmed"`) or `numbers_confirmed` stays null; `impressive.numbers_faced` uses confirmed counts only (R1).
- Budget: `game_understanding_budget_usd` default 1.50, spent under the asset's ledger key inside `cost_cap_per_asset_usd` 2.0 (D10).
- Plays are frozen per asset with `vocab_hash`; a YAML or corrections change never re-runs an analysed game unless `--force` (R6).
- Story: one chapter per play, no 45 s cap, never merged, over 360 s drop whole plays by rank; no text cards; 0.25 s dip between chapters; kaching SFX at every owner kill after remapping (D7, R2, R9).
- Montage: play windows skip `_analyze_and_window` and `_apply_window_fixes`; jump-cut gaps > 3 s between kills; only the outcome cut of a play is beat-snappable; fit by dropping plays, never `-t` truncation; month pool under `WORKSPACE/month_pool/<YYYY-MM>/` (D8, R7, R9).
- Gate: `plays_drive_cuts` default False until precision ≥ 0.8 on the labelled game (R8).

## Review Focus

1. A timeline whose owner is on team 200 (red side): `zone_relative` must flip (`blue_bot_jungle` → `enemy_jungle`) and `sides` must still list the owner among allies. Test in Task 1.
2. A kill event without `position` (older cached timelines): zone `unknown`, play still produced, no exception. Test in Task 1.
3. A VLM confirmation whose `outcome_s` lands before `engage_s` or past game end + 1: the window is rejected and the fallback used, never an inverted chapter. Test in Task 3.
4. A clip in `clips.json` overlapping two plays: `play_id` is the play containing the clip's midpoint, else the one with the larger overlap. Test in Task 2.
5. A montage plan where dropping plays by rank leaves fewer than two clips: the compiler keeps the top two and reports `over_slot=True` instead of rendering a one-clip montage. Test in Task 6.

---

### Task 1: Phases and the Riot play proposer

**Files:**
- Create: `api/src/ai_video_editor/league/play_models.py`, `api/src/ai_video_editor/league/phases.py`, `api/src/ai_video_editor/league/plays.py`, `api/src/ai_video_editor/league/map_zones.py`
- Modify: `api/src/ai_video_editor/league/riot_timeline.py:103-137` (`_champion_kill` detail), `:140-212` (`parse_timeline`, `RiotTimeline` fields)
- Test: `api/tests/league/test_phases.py`, `api/tests/league/test_plays.py`, `api/tests/league/test_riot_timeline.py` (additive)

**Interfaces:**
- Consumes: `RiotTimeline`, `TimelineEvent`, `fights()` from `league/riot_timeline.py`.
- Produces (all in `play_models.py`, pydantic, dependency-free like `lane_models.py`):
  - `PlayType = Literal["trade","gank","dive","skirmish","teamfight","objective_fight","caught_out","closeout"]`
  - `PhaseBounds(name: Literal["early","mid","late"], start_s: float, end_s: float, boundary: str)`; `Phases(early: PhaseBounds, mid: PhaseBounds, late: PhaseBounds | None)` (times are game seconds)
  - `PlayTimes(first_kill_s: float, last_kill_s: float)`
  - `PlayWindow(approach_s: float | None = None, engage_s: float | None = None, outcome_s: float | None = None, source: Literal["vlm","fallback","vlm_pending"] = "vlm_pending", offset_correction_s: float | None = None, flags: list[str] = [])` (recording seconds)
  - `PlaySides(allies: list[str], enemies: list[str], numbers: str, numbers_source: Literal["riot_lower_bound","vlm_confirmed"] = "riot_lower_bound", numbers_confirmed: str | None = None)`
  - `PlayOwner(involved: bool, role: Literal["initiator_kill","follow_up","caught_first","absent"], kills: int, assists: int, died: bool)`
  - `PlayResult(enemy_deaths: int, ally_deaths: int, outcome: Literal["won","lost","even"], objective_after: list[str], building_after: list[str])`
  - `PlayFlags(flank: bool | None = None, help_from_team: bool, soul_point: bool | None = None, steal: bool = False)`
  - `Impressive(numbers_faced: int | None = None, rank: int | None = None)`
  - `Play(id: str, type: PlayType, phase: str, game_time: PlayTimes, recording_time: PlayTimes, window: PlayWindow, zone: str, zone_relative: str, position: dict | None, sides: PlaySides, owner: PlayOwner, result: PlayResult, flags: PlayFlags, impressive: Impressive, vocab_hash: str = "", text: str = "")`
  - `ObjectiveEvent(t_s: float, type: str, sub: str | None, team: Literal["ally","enemy"])`
  - `GameModel(version: int = 3, phases: Phases | None, plays: list[Play], objectives: list[ObjectiveEvent], vocab_hash: str = "", budget_spent_usd: float = 0.0)`
  - `phases.phases_for(timeline: RiotTimeline) -> Phases | None` (None for a remake: game_end < 300 s with no kill)
  - `phases.phase_of(phases: Phases, t_game_s: float) -> str`
  - `plays.propose_plays(timeline: RiotTimeline, *, phases: Phases, gap_s: float = 15.0) -> list[Play]` (ids `p01`, `p02`… in time order; `window.source="vlm_pending"`)
  - `plays.play_text(play: Play) -> str` (the R4 template; `objective_suffix` is `", then <objective>"` for the first `objective_after` entry or `""`)
  - `plays.rank_plays(plays: list[Play]) -> list[Play]` (sets `impressive.rank`: absent last; then by `numbers_faced` desc with None last; then owner kills desc; then enemy deaths desc)
  - `map_zones.zone_of(x: float | None, y: float | None) -> str`, `map_zones.zone_relative(zone: str, owner_team_id: int | None) -> str`, `map_zones.TOWERS: dict[int, list[tuple[int,int]]]`, `map_zones.near_tower(x, y, radius: float = 800.0) -> int | None` (team id of the nearest tower within radius)
  - `riot_timeline.RiotTimeline` gains `owner_pid: int | None = None`, `owner_team_id: int | None = None`; kill `detail` gains `killer_pid`, `victim_pid`, `assist_pids`, `position` (`{"x","y"}` or None); building and elite `detail` gain `position`.

Tower coordinates (Summoner's Rift, blue = team 100), for `TOWERS`:

```python
TOWERS = {
    100: [(10504, 1029), (6919, 1483), (4281, 1253), (5846, 6396), (5048, 4812), (3651, 3696),
          (981, 10441), (1512, 6699), (1169, 4287), (1748, 2270), (2177, 1807)],
    200: [(13866, 4505), (13327, 8226), (13624, 10572), (8955, 8510), (9767, 10113), (11134, 11207),
          (4318, 13875), (7943, 13411), (10481, 13650), (12611, 13084), (13052, 12612)],
}
```

Zone geometry: map ~14820 x 14881, blue base bottom-left. `blue_base` when `x+y < 4500`, `red_base` when `x+y > 25000`, `dragon_pit` within 1500 of (9866, 4414), `baron_pit` within 1500 of (5007, 10471), `mid_lane` when `|x−y| < 1800`, `bot_lane` when `y < 2600` or (`x > 12300` and `y < 7000`), `top_lane` when `x < 2600` or `y > 12300`, river when `|x+y−14850| < 1800` (`bot_river` if `x > y` else `top_river`), else `blue_bot_jungle` / `red_bot_jungle` / `blue_top_jungle` / `red_top_jungle` by `x > y` and `x+y < 14850`. `zone_relative` maps `blue_*`/`red_*` to `ally_*`/`enemy_*` from `owner_team_id` (100 = blue) and leaves the rest unchanged.

Type rules (first match wins, evaluated on a fight of chained kills; `ed` enemy deaths, `ad` ally deaths, `n` champions who touched a kill): `closeout` if `ed >= 3` and `game_end_s - last_kill_s <= 60`; `teamfight` if `ed >= 3` and `n >= 6`; `objective_fight` if an elite kill lands within 30 s after `last_kill_s` or the first kill's zone is `dragon_pit`/`baron_pit`; `dive` if the first kill is within 800 units of a tower; `gank` if phase is early, one kill, `n == 3`, zone is a lane; `caught_out` if one kill, `ad == 1`, `ed == 0`, and the victim is the only ally among `n`; else `skirmish`. `trade` is never proposed from kills (Task 3 sweep). Owner role: `initiator_kill` when the owner killed first, `caught_first` when the owner was the first victim, `follow_up` when otherwise involved, `absent` otherwise. `flags.steal`: an elite in `objective_after` whose `killerTeamId` differs from the team with more champions in `sides`. `flags.soul_point`: a `DRAGON_SOUL_GIVEN` event within 30 s after the fight (detail from Task 1's parse: add `soul` events of kind `"elite"` with `detail["monsterType"]="DRAGON_SOUL"` and `detail["name"]`).

- [ ] **Step 1: Write the failing phase tests** (`tests/league/test_phases.py`, invented timelines built with the helpers in `tests/league/test_riot_timeline.py`)

```python
def test_early_ends_at_first_tower_but_never_before_8_min(): # tower at 360 s -> early.end_s == 480, boundary "first_tower"
def test_early_ends_at_14_min_without_tower(): # -> 840, boundary "14:00"
def test_late_starts_at_first_baron(): # baron at 1463 s -> late.start_s == 1463, boundary "first_baron"
def test_surrender_before_late_has_two_phases(): # game_end 1100, no baron/inhib -> late is None, mid.end_s == 1100
def test_remake_has_no_phases(): # game_end 240, no kills -> None
def test_phase_of(): # 100 -> early, 900 -> mid, 1600 -> late; 1600 with late None -> mid
```

- [ ] **Step 2: Run them to verify they fail** — `pytest tests/league/test_phases.py -q` → ImportError.
- [ ] **Step 3: Implement `play_models.py`, `map_zones.py`, `phases.py`** per the Interfaces block.
- [ ] **Step 4: Run the phase tests** → PASS. Commit: `feat(league): play models, map zones, event-driven phases`.
- [ ] **Step 5: Write the failing timeline-detail tests** (append to `tests/league/test_riot_timeline.py`): `test_kill_detail_carries_pids_and_position` (`detail["killer_pid"] == 1`, `detail["position"] == {"x": 8536, "y": 7611}`), `test_kill_without_position_has_none`, `test_timeline_knows_owner_pid_and_team` (`owner_pid == 1`, `owner_team_id == 100`), `test_soul_event_parsed` (`DRAGON_SOUL_GIVEN` → kind `elite`, `detail["monsterType"] == "DRAGON_SOUL"`).
- [ ] **Step 6: Run** → FAIL (KeyError / AttributeError).
- [ ] **Step 7: Modify `riot_timeline.py`**: `_champion_kill` returns the extra detail keys; `parse_timeline` passes `position` for building/elite events, parses `DRAGON_SOUL_GIVEN`, and sets `owner_pid`, `owner_team_id` on the returned `RiotTimeline`. Existing tests must stay green (additive only).
- [ ] **Step 8: Run `pytest tests/league -q`** → PASS. Commit: `feat(league): kill pids, positions, soul events on the timeline`.
- [ ] **Step 9: Write the failing proposer tests** (`tests/league/test_plays.py`; one invented 10-participant match, owner pid 1 team 100 in one fixture and owner pid 6 team 200 in a second)

```python
def test_teamfight_with_sides_numbers_and_initiator(): # 4 enemy deaths, 1 ally death, 9 touched, owner first killer -> type teamfight, numbers "5v4", numbers_source riot_lower_bound, role initiator_kill, outcome won
def test_objective_fight_from_elite_after(): # dragon 12 s after last kill -> objective_fight, objective_after == ["DRAGON/AIR_DRAGON"]
def test_closeout_near_game_end(): # 3 enemy deaths 40 s before game_end -> closeout
def test_gank_early_three_champions_in_lane(): # -> gank
def test_caught_out_single_ally_death(): # victim alone, no enemy death -> caught_out, owner role caught_first when owner is the victim
def test_dive_near_tower(): # kill at (10400, 1100) blue bot outer -> dive
def test_absent_owner_play_is_kept_and_tagged(): # role absent, involved False
def test_zone_relative_flips_for_red_side_owner(): # owner team 200, kill in blue_bot_jungle -> zone_relative "enemy_jungle"
def test_kill_without_position_gets_unknown_zone(): # no exception
def test_play_text_template(): # "5v4 teamfight in mid_lane, owner initiator_kill, won 4-for-1" and with objective ", then DRAGON/AIR_DRAGON"
def test_rank_plays_orders_absent_last_then_numbers_faced(): # ranks 1..n set
def test_example_fixture_validates_as_game_model(): # tests/fixtures/plays/jhin_example.json (copy of docs/specs/examples/digest-v3-game-example.json) -> GameModel.model_validate(data["game"]) succeeds; 21 plays, 13 with owner.involved, p15 type "teamfight" numbers "5v4"
```

The example fixture's `plays[*]` must validate against `Play` as defined above (the example has no `numbers_source` on some fields; defaults cover it). The real `match.json`/`timeline.json` are never committed.

- [ ] **Step 10: Run** → FAIL.
- [ ] **Step 11: Implement `plays.py`** (`propose_plays`, `play_text`, `rank_plays`, private `_classify`, `_sides`, `_owner_role`, `_after`).
- [ ] **Step 12: Run `pytest tests/league -q`** → PASS; `ruff check`. Commit: `feat(league): Riot play proposer with sides, zones, roles, outcomes`.

### Task 2: Play vocabulary, digest v3 and clip linkage

**Files:**
- Create: `api/src/ai_video_editor/vlm/game_hints/league_plays.yaml`, `api/src/ai_video_editor/league/play_vocab.py`, `api/src/ai_video_editor/sfx_library.py`
- Modify: `api/pyproject.toml` (add `pyyaml>=6.0` to dependencies), `api/src/ai_video_editor/grokbot/digest.py:44-97` (models), `:286-371` (`build_digest` signature + linkage), `api/src/ai_video_editor/config.py` (new settings), `api/tests/conftest.py` (`_pinned_settings`)
- Test: `api/tests/test_play_vocab.py`, `api/tests/test_grokbot_digest_v3.py`, `api/tests/test_sfx_library.py`

**Interfaces:**
- Consumes: `GameModel`, `Play` (Task 1).
- Produces:
  - `play_vocab.PlayRule(type: str, phase: list[str], propose: str, start: dict[str, str], end: dict[str, str | float], vlm_confirms: list[str], facts_from_riot: list[str], examples: list[str] = [])`; `PlayVocabulary(rules: dict[str, PlayRule], hash: str)`; `load_vocab(game: str | None = "league") -> PlayVocabulary` (reads `settings.game_hints_dir / "league_plays.yaml"` when present, else the packaged file; `hash = sha1(file bytes)[:12]`).
  - `digest.Digest` gains `version: int = 3`, `game: GameModel | None = None`; `DigestClip` gains `play_id: str | None = None`.
  - `digest.attach_plays(digest: Digest, game: GameModel) -> Digest` (sets `digest.game`, fills `play_id` per the Review Focus 4 rule using clip `start_s`/`end_s` against each play's `recording_time` first/last kill, and sets `play.text = play_text(play)` for every play; never raises).
  - `build_digest(..., game_model: GameModel | None = None)` calls `attach_plays` when given.
  - Settings: `game_understanding_budget_usd: float = 1.5`, `plays_drive_cuts: bool = False`, `play_confirm_fps: float = 1.0`, `play_sweep_fps: float = 0.25`, `play_sweep_enabled: bool = True`, `story_transition_s: float = 0.25`, `story_kill_sfx: bool = True`, `montage_jump_cut_gap_s: float = 3.0`; all pinned in `conftest._pinned_settings`.
  - `sfx_library.kaching_path(*, run=subprocess.run) -> Path`: `settings.workspace_dir / "sfx" / "kaching.wav"`; when absent, synthesises a 0.35 s two-tone chime with ffmpeg (`-f lavfi -i "aevalsrc=0.6*sin(2*PI*1760*t)*exp(-8*t)+0.4*sin(2*PI*2637*t)*exp(-6*t):s=48000:d=0.35"`, `-ac 2`) and writes it; the owner may replace the file; `kaching_cmd(out: Path) -> list[str]` is the pure argv builder. Test `test_sfx_library.py`: `test_kaching_cmd_argv`, `test_kaching_path_synthesised_when_absent` (fake `run` records argv and touches the file), `test_kaching_path_reuses_existing_file`.
- YAML content: the eight rules from the spec (4.2 type rules in `propose`, `start: {story: approach, montage: engage, fallback: "first_kill - 8"}`, `end` per type: teamfight/skirmish/objective_fight/closeout `{rule: outcome, max_after_last_kill_s: 20}`, gank `{rule: outcome, max_after_last_kill_s: 10}`, dive `{rule: outcome, max_after_last_kill_s: 15}`, caught_out `{rule: "death + 2", max_after_last_kill_s: 2}`, trade `{rule: "both back off", max_after_last_kill_s: 8}`).

- [ ] **Step 1: Failing vocab tests**: `test_packaged_vocab_has_eight_rules`, `test_workspace_vocab_overrides_packaged` (tmp `game_hints_dir` with a two-rule file → 2 rules, different hash), `test_hash_changes_with_content`.
- [ ] **Step 2: Run** → FAIL. **Step 3:** `uv add pyyaml`; write the YAML and `play_vocab.py`. **Step 4: Run** → PASS. Commit: `feat(vlm): league play vocabulary YAML + loader`.
- [ ] **Step 5: Failing digest tests** (`tests/test_grokbot_digest_v3.py`, reuse the panel_round fixture in `tests/fixtures/panel_round`):

```python
def test_v2_digest_json_loads_with_game_absent(): # Digest.model_validate(old dict) -> game None, version 3
def test_attach_plays_links_clip_by_midpoint_then_overlap(): # clip 1390-1415 over p15 (1395-1410) and p14 (1310-1312) -> play_id "p15"
def test_play_text_is_templated_and_passes_leak_check(): # transcript containing "jhin kills miss fortune in mid lane" -> digest_leaks == []
def test_digest_json_is_pretty(): # model_dump_json(indent=2) has one key per line: '"version": 3' on its own line
```

- [ ] **Step 6: Run** → FAIL. **Step 7:** modify `digest.py` and `config.py`, pin settings in conftest. **Step 8: Run `pytest tests/test_grokbot_digest*.py tests/test_grokbot_publish.py -q`** → PASS. Commit: `feat(digest): v3 game section, play_id linkage, templated play text`.

### Task 3: VLM play confirmer and the understanding pass

**Files:**
- Create: `api/src/ai_video_editor/vlm/play_confirm.py`, `api/src/ai_video_editor/league/understand.py`, `api/src/ai_video_editor/league/understand_cli.py`
- Modify: `api/src/ai_video_editor/anthropic_surface.py` (add `spent_usd`), `api/pyproject.toml` (script `league-understand = "ai_video_editor.league.understand_cli:main"`)
- Test: `api/tests/test_play_confirm.py`, `api/tests/league/test_understand.py`

**Interfaces:**
- Consumes: `propose_plays`, `rank_plays`, `phases_for` (Task 1); `load_vocab` (Task 2); `vlm.backends.base.run_with_retry`, `vlm.backends.select_backend` (as used by `clip_analyzer.analyze_source_clip`); `vlm.watch_montage._compose_mosaic`; `anthropic_surface.check_budget`, `SpendCapError`.
- Produces:
  - `play_confirm.PlayConfirmation(BaseModel)`: `approach_s: float | None`, `engage_s: float | None`, `outcome_s: float | None`, `first_banner_s: float | None`, `allies_seen: int | None`, `enemies_seen: int | None`, `flank: bool | None`, `description: str`, `confidence: float` (all seconds relative to the sampled window start).
  - `play_confirm.sample_window(video_path: str, out_dir: Path, *, start_s: float, end_s: float, fps: float) -> list[Path]` (ffmpeg `-ss start -t (end-start) -vf fps=…,scale=480:-2`, then 2x2 mosaics via `_compose_mosaic`).
  - `play_confirm.confirm_play(video_path: str, play: Play, *, vocab: PlayVocabulary, backend, fps: float, lead_s: float = 30.0, tail_s: float = 20.0) -> tuple[PlayConfirmation | None, float]` (returns the confirmation and the window start in recording seconds; None when the backend is unavailable or parsing fails).
  - `play_confirm.apply_confirmation(play: Play, conf: PlayConfirmation | None, window_start_s: float, *, game_end_s: float | None, rule: PlayRule) -> Play` (pure): converts to recording seconds; banner within ±5 s of `recording_time.first_kill_s` → `offset_correction_s`, else flag `offset_uncertain`; missing/invalid approach → fallback `first_kill_s − 8` with flag `approach_unconfirmed`; `engage_s` defaults to `approach_s` when missing; `outcome_s` clamped to `last_kill_s + max_after_last_kill_s` and `game_end_s + 1`, and must be > `engage_s` else fallback `last_kill_s + 3`; `numbers_confirmed = f"{allies_seen}v{enemies_seen}"` and `numbers_source="vlm_confirmed"` only when both counts are present; `impressive.numbers_faced = enemies_seen − (allies_seen − 1)` when the owner is involved; `flags.flank` copied; `window.source = "vlm"` or `"fallback"`.
  - `play_confirm.PlaySweepFinding(BaseModel)`: `t_s: float`, `type: Literal["trade","caught_out"]`, `description: str`, `confidence: float`; `sweep_game(video_path, *, duration_s, fps, backend, vocab) -> list[PlaySweepFinding]` (panels at `fps`, one call per 30 panels, prompt from the `trade` and `caught_out` rules); findings with confidence ≥ 0.6 become `Play(type=…, window from t_s−10 to t_s+8, source "vlm", sides empty, numbers "?v?")`.
  - `anthropic_surface.spent_usd(asset_id: str, *, feature_prefix: str, db_path: Path | None = None) -> float`.
  - `understand.understand_game(asset_id: str, video_path: str, timeline: RiotTimeline, *, vocab: PlayVocabulary, budget_usd: float, fps: float, sweep_fps: float, sweep: bool, force: bool = False, out_path: Path | None = None) -> GameModel` (`out_path` defaults to `WORKSPACE/plays/<asset_id>.json`; the eval harness passes its own): loads the file; sets every play's `text` with `play_text` after confirmation; and returns it when `vocab_hash` matches and not `force`; else proposes, confirms each play in rank order while `spent_usd(asset_id, feature_prefix="play_") < budget_usd` (plays past the budget keep `vlm_pending` and get the fallback window), runs the sweep when enabled and ≥ $0.25 remains, ranks, writes the file with `indent=2` plus a sidecar `WORKSPACE/plays/<asset_id>.prose.json` holding each play's `description` (never merged into the digest).
  - CLI `league-understand --asset ID [--source PATH] [--force] [--fps F] [--no-sweep]` prints `phases`, one line per play (`id type phase zone_relative numbers role outcome window`), and `budget_spent_usd`.
- The backend is created with `select_backend(asset_id=asset_id)` so spend lands on the asset ledger under feature names `play_confirm` and `play_sweep` (pass `feature=` if the backend supports it, else wrap `record_spend` as the analyzer does).

- [ ] **Step 1: Failing `apply_confirmation` tests** (pure, no ffmpeg): `test_window_from_confirmation_in_recording_seconds`, `test_banner_offset_correction_stored` (banner at 31.5 in a window starting at `first_kill−30` → `offset_correction_s == 1.5`), `test_banner_missing_flags_offset_uncertain`, `test_missing_approach_falls_back_8s_and_flags`, `test_outcome_before_engage_rejected` (Review Focus 3), `test_outcome_clamped_to_game_end_plus_one`, `test_numbers_confirmed_only_with_both_counts`, `test_numbers_faced_uses_confirmed_counts`.
- [ ] **Step 2: Run** → FAIL. **Step 3:** implement `play_confirm.py` models and `apply_confirmation`. **Step 4: Run** → PASS. Commit.
- [ ] **Step 5: Failing confirmer/sweep tests with a fake backend** (`backend.call` returns canned JSON; `sample_window` monkeypatched to return two tiny JPEG paths): `test_confirm_play_calls_backend_once_with_rule_prompt` (system prompt contains the rule's `end.rule` text), `test_confirm_play_returns_none_on_unavailable`, `test_sweep_findings_become_trade_plays_above_confidence`.
- [ ] **Step 6: Run** → FAIL. **Step 7:** implement `confirm_play`, `sample_window`, `sweep_game`, prompts (League-specific; mention health-bar colours, kill banner, scoreboard not a defect). **Step 8: Run** → PASS. Commit: `feat(vlm): play confirmer, banner re-anchor, kill-less sweep`.
- [ ] **Step 9: Failing `understand_game` tests** (tmp workspace, fake backend, ledger in a tmp sqlite via `db_path`): `test_frozen_file_returned_when_hash_matches`, `test_force_reruns`, `test_budget_stops_confirmations_and_leaves_pending` (budget 0.00 → every play `source == "fallback"`, flags include `approach_unconfirmed`, file still written), `test_plays_file_is_pretty_and_prose_is_separate`.
- [ ] **Step 10: Run** → FAIL. **Step 11:** implement `spent_usd`, `understand.py`, the CLI, the pyproject script. **Step 12: Run `pytest tests/test_play_confirm.py tests/league -q`** → PASS; ruff. Commit: `feat(league): understanding pass, frozen plays file, league-understand CLI`.

### Task 4: Labels and the precision gate

**Files:**
- Create: `api/src/ai_video_editor/league/plays_eval.py`, `api/src/ai_video_editor/league/plays_eval_cli.py`, `api/tests/fixtures/plays/labels_example.json`
- Modify: `api/pyproject.toml` (script `league-plays-eval = "ai_video_editor.league.plays_eval_cli:main"`)
- Test: `api/tests/league/test_plays_eval.py`

**Interfaces:**
- Consumes: `GameModel`, `Play` (Task 1); `understand_game` (Task 3) for the fps harness.
- Produces: `LabelRow(type: str, approach_s: float, engage_s: float, outcome_s: float, numbers: str | None = None, flank: bool | None = None, note: str = "")`; `Labels(asset_id: str, rows: list[LabelRow])`; `match_plays(detected: list[Play], labels: list[LabelRow], *, iou_min: float = 0.5, start_tol_s: float = 5.0) -> EvalReport(precision: float, recall: float, matched: list[tuple[str, int]], unmatched_detected: list[str], unmatched_labels: list[int], per_type: dict[str, dict[str, float]], type_confusion: list[tuple[str, str]])`. A detected play's window is `[approach_s or fallback, outcome_s]`; a label's is `[approach_s, outcome_s]`; a match needs IoU ≥ `iou_min` and `|approach − approach| ≤ start_tol_s`; each label matches at most one play (greedy by IoU). `passes_gate(report, min_precision: float = 0.8) -> bool`.
- CLI `league-plays-eval --asset ID [--labels PATH] [--fps 0.25,1.0 --source PATH]`: prints precision, recall, per-type table, confusions; with `--fps` runs `understand_game(force=True)` per fps into `WORKSPACE/plays/<asset>.fps<f>.json` and prints cost and precision per setting.

- [ ] **Step 1: Failing tests**: `test_match_by_iou_and_start_tolerance`, `test_label_matches_at_most_one_play`, `test_start_off_by_six_seconds_is_unmatched`, `test_type_confusion_counted_when_window_matches_but_type_differs` (matched for precision, listed in `type_confusion`), `test_gate_at_0_8`, `test_labels_example_fixture_loads`.
- [ ] **Step 2: Run** → FAIL. **Step 3:** implement `plays_eval.py`, fixture, CLI, script entry. **Step 4: Run** → PASS; ruff. Commit: `feat(league): labels schema, play matcher, precision gate, league-plays-eval`.

### Task 5: Story planner v2 and renderer transitions

**Files:**
- Modify: `api/src/ai_video_editor/story/planner.py` (add `plan_story_from_plays`; `Chapter` gains `play_id: str | None = None`, `kill_times_s: list[float] = []`), `api/src/ai_video_editor/story/render.py:60-110` (`chapter_cmd` fades), `:200-260` (`render_story` SFX pass, no cards when `plan.cards is False`), `api/src/ai_video_editor/story/cli.py` (`--plays` flag)
- Test: `api/tests/test_story_planner_plays.py`, `api/tests/test_story_render_transitions.py`

**Interfaces:**
- Consumes: `GameModel`, `Play`, `rank_plays` (Task 1); `sfx_library.kaching_path()` (Task 2); `edits.apply_sfx(source_path, out_path, *, sfx=[{sfx_path, at_seconds, volume}])`; `settings.story_transition_s`, `settings.story_kill_sfx`.
- Produces:
  - `planner.plan_story_from_plays(game: GameModel, timeline: RiotTimeline, *, mic_stream: int | None, max_s: float = 360.0, asset_id: str = "") -> StoryPlan`: chapters are the owner-involved plays (`offset_uncertain` plays stay in: R5 excludes them from shorts and montage only); each chapter `start_s = window.approach_s or first_kill_s − 8`, `end_s = window.outcome_s or last_kill_s + 3`, clamped to `[0, game_end_s + 1]`; `kind` = `"lane"` for plays in phase `early`, `"close"` for `closeout`, else `"fight"`; `title` = the play's `text` (kept in `story.json`, never rendered); `play_id` set; `kill_times_s` = recording times of the owner's kills inside the chapter (from `timeline.events` kind `kill`); no merging; sorted by `start_s`; over `max_s` drop the lowest-ranked chapter that is not `close` until it fits (lane chapters are droppable after fights); `StoryPlan.cards: bool = False` new field.
  - `render.chapter_cmd(..., fade_s: float = 0.0)`: with `fade_s > 0` the video chain appends `,fade=t=in:st=0:d={fade_s},fade=t=out:st={dur−fade_s}:d={fade_s}` and the audio leg appends `afade=t=in:st=0:d={fade_s},afade=t=out:st={dur−fade_s}:d={fade_s}` before `[a]`.
  - `render.render_story(..., cards=…)`: when `plan.cards` is False, no card parts; after each chapter part is encoded, when `settings.story_kill_sfx` and `ch.kill_times_s`, `apply_sfx` overlays the kaching at `t − ch.start_s` for each kill into `chapter_XX_sfx.mp4`, which replaces the part; fades use `settings.story_transition_s`.
  - CLI: `story-render --plays` loads `WORKSPACE/plays/<asset>.json` (`GameModel`), calls `plan_story_from_plays`, forces no cards; without `--plays` the v2 path is unchanged.

- [ ] **Step 1: Failing planner tests**: `test_one_chapter_per_play_no_merge` (two plays 30 s apart → two chapters), `test_no_45s_cap` (a 58 s window stays 58 s), `test_drop_whole_plays_by_rank_until_fit` (max_s small → lowest rank dropped, chapters never trimmed), `test_absent_plays_excluded`, `test_kill_times_inside_chapter`, `test_fallback_window_when_vlm_pending`, `test_close_kind_for_closeout`, `test_titles_are_play_text_and_cards_false`.
- [ ] **Step 2: Run** → FAIL. **Step 3:** implement `plan_story_from_plays`. **Step 4: Run `pytest tests/test_story*.py -q`** → PASS (existing `plan_story` tests untouched). Commit: `feat(story): chapters 1:1 from plays, drop by rank, no cards`.
- [ ] **Step 5: Failing render tests** (argv-only, no ffmpeg; `_run_ffmpeg`, `apply_sfx` and `kaching_path` monkeypatched): `test_chapter_cmd_has_fades_when_transition_set` (`fade=t=out:st=19.75:d=0.25` for a 20 s chapter), `test_render_story_skips_cards_when_plan_cards_false`, `test_kaching_applied_at_kill_offsets` (`apply_sfx` called with `at_seconds == [12.4]` for a kill at 1407.4 in a chapter starting 1395.0).
- [ ] **Step 6: Run** → FAIL. **Step 7:** modify `render.py` and `cli.py`. **Step 8: Run** → PASS; ruff. Commit: `feat(story): dip transitions, kaching on owner kills, --plays`.

### Task 6: Montage v2 on play windows

**Files:**
- Create: `api/src/ai_video_editor/montage/__init__.py`, `api/src/ai_video_editor/montage/plan.py`, `api/src/ai_video_editor/montage/month_pool.py`
- Modify: `api/src/ai_video_editor/shorts.py:160-200` (`ShortClip.play_id: str | None = None`, `ShortClip.locked_window: bool = False`), `:1296-1340` (`_apply_window_fixes` skips locked clips), `:2230-2269` (`_analyze_and_window` not called for locked clips), `:3136-3290` (`build_custom_montage(play_specs=…)`), `api/src/ai_video_editor/routers/shorts.py:538-650` (`CustomMontageBody.play_specs`), `api/src/ai_video_editor/compile_cleanup.py` (test only)
- Test: `api/tests/test_montage_plan.py`, `api/tests/test_month_pool.py`, `api/tests/test_custom_montage_plays.py`

**Interfaces:**
- Consumes: `Play`, `rank_plays` (Task 1); `sfx_library.kaching_path()` (Task 2); `trim_clip` (as imported in `shorts.py`); `_snap_plan_to_beats` semantics (only clips with `anchor_offset_seconds` snap); `_kill_times_in_montage`; `edits.apply_sfx`; `settings.montage_jump_cut_gap_s`. This task depends on Tasks 1 and 2 only.
- Produces:
  - `montage.plan.Segment(source: str, start_s: float, end_s: float, play_id: str, is_last: bool, kill_offsets_s: list[float])`.
  - `montage.plan.jump_cut_segments(play: Play, kill_times_s: list[float], *, source: str, gap_s: float = 3.0, lead_s: float = 3.0) -> list[Segment]`: the play runs `engage_s − lead_s` to `outcome_s`; a gap between consecutive kills longer than `gap_s` is cut to `[kill_i + 1.0, kill_{i+1} − 1.5]` removed (so each segment ends 1 s after a kill and the next starts 1.5 s before the next kill); the last segment ends at `outcome_s` and has `is_last=True`.
  - `montage.plan.fit_plays(plays: list[Play], *, slot_s: float, kill_times: dict[str, list[float]], sources: dict[str, str], gap_s: float) -> MontagePlan(segments: list[Segment], dropped: list[str], over_slot: bool)`: ranks plays, builds segments, drops the lowest-ranked play while the total exceeds `slot_s`, keeps at least two plays (`over_slot=True` when still over), orders rising → drop → falling by rank (best play in the middle, as `music/arc.py` does for kills).
  - `montage.month_pool.pool_dir(month: str | None = None) -> Path` (`WORKSPACE/month_pool/YYYY-MM`); `pool_play(asset_id: str, play: Play, source: str, *, pad_s: float = 10.0) -> Path | None` (cuts `[approach or first_kill−8 −pad, outcome or last_kill+3 +pad]` to `<asset>_<play_id>.mp4` via `trim_clip`, skips when the file exists, writes `index.json` with asset, play id, window, rank); eligible types: `teamfight`, `objective_fight`, `closeout`, and `skirmish` with ≥ 3 enemy deaths; `offset_uncertain` plays are never pooled; `clear_pool(month)`.
  - `shorts.build_custom_montage(..., play_specs: list[dict] | None = None)`: each spec `{clip_path, start_seconds, end_seconds, play_id, is_last, kill_offsets_s}` becomes a `ShortClip` with `locked_window=True`, `start/end` as given, `anchor_offset_seconds = kill_offsets_s[-1]` when `is_last` else `None` (so only the outcome cut snaps, R9); locked clips bypass the analyzer and every window fix; after the final render, when any spec carries kill offsets, the kaching is overlaid at `_kill_times_in_montage`-style remapped times (`montage_t + offset` per segment) through `apply_sfx` into the same output path.
  - Router: `CustomMontageBody.play_specs: list[dict] | None = None` passed through.
  - The MCP wrapper for `play_specs` is out of scope (sibling repo); the API field is enough for the runner script.

- [ ] **Step 1: Failing plan tests**: `test_segments_keep_engage_and_outcome`, `test_gap_over_3s_is_cut_with_1s_after_and_1_5s_before`, `test_only_last_segment_snappable`, `test_fit_drops_lowest_rank_until_slot`, `test_fit_keeps_two_plays_and_flags_over_slot` (Review Focus 5), `test_order_rising_drop_falling`.
- [ ] **Step 2: Run** → FAIL. **Step 3:** implement `montage/plan.py`. **Step 4: Run** → PASS. Commit.
- [ ] **Step 5: Failing pool tests** (`trim_clip` monkeypatched): `test_pool_writes_padded_clip_and_index`, `test_pool_skips_existing_and_uncertain`, `test_pool_dir_is_not_a_compilation_folder` (`compile_cleanup.is_safe_compilation_folder(pool_dir())` is False). **Step 6: Run** → FAIL. **Step 7:** implement `month_pool.py`. **Step 8: Run** → PASS. Commit.
- [ ] **Step 9: Failing compiler tests** (`render_short`, `_run_vlm_review`, `apply_sfx` monkeypatched as in `tests/test_beat_snap_ends.py`): `test_locked_clips_skip_analyzer_and_fixes` (analyzer not called; a `trim_end` fix leaves the window unchanged), `test_only_last_segment_has_anchor`, `test_kaching_overlaid_at_remapped_times`, `test_router_accepts_play_specs`.
- [ ] **Step 10: Run** → FAIL. **Step 11:** modify `shorts.py` and the router. **Step 12: Run `pytest tests/test_custom_montage_plays.py tests/test_beat_snap_ends.py tests/test_plan_router.py -q`** → PASS; ruff. Commit: `feat(montage): play windows authoritative, jump cuts, outcome-only snap, month pool`.

### Task 7: Run — label the Jhin game, pass the gate, re-render

**Files:**
- Create: `WORKSPACE/labels/42672a10-5b17-4f60-9231-933b3c90ab97.json` (with the owner), `docs/grokbot/game-understanding.md` (how to run `league-understand`, `league-plays-eval`, `story-render --plays`, the montage runner script)
- Scratchpad: `montage_plays_run.py` (builds `play_specs` from the month pool and posts to `/api/v1/shorts/custom_montage`)

- [ ] **Step 1:** `league-understand --asset 42672a10… --fps 1.0` and `--fps 0.25 --force` into separate files; record cost per setting.
- [ ] **Step 2 (owner present):** watch the Jhin game together and write the labels file (type, approach, engage, outcome, numbers, flank per play).
- [ ] **Step 3:** `league-plays-eval --asset … --fps 0.25,1.0`; pick the cheapest fps with precision ≥ 0.8; set `PLAY_CONFIRM_FPS` by appending to `api/.env` (never read it); flip `PLAYS_DRIVE_CUTS=true` the same way only when the gate passes.
- [ ] **Step 4:** `story-render --asset … --round 2026-11-20_2001_jhin_8-2-13 --plays --review`; upload `story_jhin_v3_<date>.mp4` to Drive `renders/story/`.
- [ ] **Step 5:** `league-understand` for the Corki, Zeri and Ezreal assets; pool November; run the montage script with `max_seconds=90` and the royalty-free track; upload `montage_nov20_v2_<date>.mp4`.
- [ ] **Step 6:** Republish the four digests (v3) through `grokbot-publish-round` with the game model attached; write the docs page; commit.
