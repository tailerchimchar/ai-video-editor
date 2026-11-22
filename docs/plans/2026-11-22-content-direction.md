# Content Direction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build experiment 1 (a 4-6 min story video of the Jhin game carried by the owner's mic), a Nov 20 montage, lane data, a commentary bucket, a per-game scorecard, and the pipeline fixes the Jhin and Corki rounds exposed.

**Architecture:** New modules sit beside the existing pipeline: `candidates/mic_track.py` finds and transcribes the owner's mic stream into `transcripts` rows tagged `speaker='owner'`; `league/lane.py` and `league/scorecard.py` read the cached Riot timeline; `story/` plans chapters from expert beats plus the timeline and renders them per chapter with ffmpeg, then concatenates; the montage reuses `shorts.build_custom_montage` with a `9:16_hud` layout and ends-only beat snap. Fixes land in `highlights.py`, `league/candidates/riot.py`, `edits.py` and `grokbot/digest.py`.

**Tech Stack:** Python 3.11, FastAPI, pydantic v2, httpx only, SQLite (`DATABASE_PATH` absolute), ffmpeg/ffprobe, faster-whisper, numpy, librosa (already present), uv.

**Spec:** `docs/specs/spec-content-direction.md` (D1-D12, R1-R10). Jira epic AVE-36.

## Global Constraints

- Keys only in `api/.env`; never in logs, digests, Drive, git. `uv run --no-sync secret-scan` clean before every merge. Never read or copy `api/.env`.
- Mic transcripts, narration and `owner_recs/` music stay local; Drive gets paraphrase, royalty-free or silent renders only (R5).
- Opponents are named by champion only; participant index checked against champion id (R9).
- Fight window rules: 8.0 s pre-pad before a fight that opens with an ally death, else 4.0 s; ranker may widen at most 5.0 s per side; end never past `game_end_s + 1.0`.
- Commentary: at most 5 items per game, separate digest section (R7). Story review budget $5.00 per video, outside the $2.00 asset cap (R3).
- Tests: `uv run --no-sync pytest -q` from `api/` (worktrees: main venv python with `PYTHONPATH=<worktree>/api/src`); `uv run --no-sync ruff check . && ruff format --check .`. Tests must not read the live `.env` (conftest pins settings).
- Commit messages end with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

## Review Focus

1. A recording with ONE audio stream (mic mixed in): `pick_mic_stream` must return None, the story renders with game audio only and `story.json` carries `mic_stream: null, audio: "mixed"`. Test in Task 2 and Task 5.
2. A fight opening with an ally death in the first 8 s of the recording: the window start clamps to 0.0 and the end still obeys the rules. Test in Task 1.
3. Two expert beats inside the same fight: the planner merges them into one chapter, never two overlapping cuts. Test in Task 5.
4. A montage rendered with a track from `owner_recs/`: `publish` refuses the Drive upload with a clear reason. Test in Task 6.
5. An existing database without the `speaker` column: the migration adds it and old rows read as mixed audio (`speaker IS NULL`). Test in Task 2.

---

### Task 1: Pipeline fixes (pre-pad by role, widen cap, end cap, encoder opts, voice ref fallback, CV advisory)

**Files:**
- Modify: `api/src/ai_video_editor/highlights.py:154-222` (`fight_window`)
- Modify: `api/src/ai_video_editor/league/candidates/riot.py:453-466` (fight bounds metadata)
- Modify: `api/src/ai_video_editor/edits.py` (every hard-coded `-preset`/`-crf` in `_apply_with_hud`, `apply_zoom`, `apply_caption`, `apply_focus`, `apply_sfx`)
- Create: `api/src/ai_video_editor/refs.py`
- Modify: `api/src/ai_video_editor/grokbot/digest.py:96-112,151-182` and `api/src/ai_video_editor/decisions/scorecard.py` (use `refs.normalize_clip_ref`)
- Modify: `api/src/ai_video_editor/candidates/service.py` (CV advisory)
- Test: `api/tests/test_highlights_windows.py`, `api/tests/test_edits_encoder.py`, `api/tests/test_refs.py`, `api/tests/test_digest_voice_fallback.py`, `api/tests/test_cv_advisory.py`

**Interfaces:**
- Produces: `highlights.FIGHT_PRE_ALLY_DEATH_S = 8.0`, `highlights.RANKER_WIDEN_MAX_S = 5.0`; candidate `metadata["fight_opens_with_ally_death"]: bool`; `refs.normalize_clip_ref(ref: str) -> str` returning the `NmNNs` form (`"06_kill_23m05s.mp4"`, `"23m05s"`, `"1385.0"`, `"23:05"` all → `"23m05s"`); candidate `metadata["riot_disagrees"]: bool`.

- [ ] **Step 1: Write the failing window tests** in `test_highlights_windows.py`:
  - `test_pre_pad_8s_when_fight_opens_with_ally_death`: meta `{fight_start_s: 100, fight_end_s: 110, fight_opens_with_ally_death: True}` → start `92.0`.
  - `test_pre_pad_4s_otherwise` → start `96.0`.
  - `test_ranker_widen_capped_at_5s`: suggested `(80.0, 130.0)` with the 4 s meta → `(91.0, 118.0)`.
  - `test_end_never_past_game_end_plus_1`: `game_end_s: 112` and suggested end `130` → end `113.0`.
  - `test_ally_death_at_recording_start_clamps_to_zero`: `fight_start_s: 3` → start `0.0`.
  - `test_outplayed_only_path_unchanged`: no fight bounds → identical to the current `_window_anchor` result (pin with the existing fixture).
- [ ] **Step 2: Run them, expect FAIL** (`uv run --no-sync pytest -q tests/test_highlights_windows.py`).
- [ ] **Step 3: Implement** in `fight_window`: `pre = 8.0 if meta.get("fight_opens_with_ally_death") else float(meta.get("pre_pad_s", FIGHT_PRE_S))`; widening `start = max(start - RANKER_WIDEN_MAX_S, min(start, suggested[0]))`, symmetric for the end; after the game-end extension and widening, `if game_end is not None: end = min(end, float(game_end) + 1.0)`. In `riot.py`, when attaching `fight_start_s`/`fight_end_s`, set `meta["fight_opens_with_ally_death"] = f.events[0].kind in ("ally_death", "death")`.
- [ ] **Step 4: Run, expect PASS. Commit** `fix(windows): pre-pad by role, cap ranker widening, end at game_end+1`.
- [ ] **Step 5: Encoder opts test** `test_edits_use_configured_codec_opts`: monkeypatch `settings.ffmpeg_video_codec_opts = "-preset slow -crf 18"`, capture the ffmpeg argv `apply_caption` builds (monkeypatch `edits._run_ffmpeg` to record and return `(True, None)`), assert `"slow"` and `"18"` present and no `veryfast`. Run → FAIL. Replace every hard-coded preset/crf in `edits.py` with `*settings.ffmpeg_video_codec_opts.split()`. Run → PASS. Commit `fix(edits): honour FFMPEG_VIDEO_CODEC_OPTS`.
- [ ] **Step 6: Refs test** `test_refs.py` with the four forms above plus `"7m27s"` ↔ `"07m27s"` both → `"7m27s"`. Run → FAIL. Implement `normalize_clip_ref` in `refs.py` (regex on `(\d+)m(\d+)s`, `mm:ss`, bare seconds). Replace the private normaliser in `decisions/scorecard.py` and use it in `digest._by_ref` and `digest._voice` beat lookup. Test `test_digest_voice_falls_back_to_beat_with_expert_ref`: gaming-expert entry keyed `"06_kill_23m05s"` with `story_beats[0].beat = "Neeko pick"` and clip ref `"23m05s"`, no paraphrase → `voice == "Neeko pick"`. Run → PASS. Commit `fix(digest): voice beat fallback matches expert refs`.
- [ ] **Step 7: CV advisory test** `test_cv_kda_demoted_when_riot_disagrees`: a `cv_kda` candidate at 220-240 s, Riot timeline (confidence high) with no kill/assist/death in that window → candidate `confidence` halved and `metadata["riot_disagrees"] is True`; with a kill inside → unchanged. Run → FAIL. Implement in `candidates/service.py` after the Riot source runs (only when the timeline confidence is `high`). Run → PASS. Commit `fix(candidates): cv_kda advisory when Riot disagrees`.
- [ ] **Step 8: Full suite + ruff + secret-scan green.**

### Task 2: Mic track probe, pick and owner transcript

**Files:**
- Create: `api/src/ai_video_editor/candidates/mic_track.py`
- Modify: `api/src/ai_video_editor/database.py:101-110` (add `speaker TEXT` with an idempotent `ALTER TABLE` migration in the init path)
- Modify: `api/src/ai_video_editor/candidates/transcribe_backends.py:166-196` (`_extract_audio` gains `stream_index: int | None`)
- Modify: `api/src/ai_video_editor/routers/analyze.py` (new `POST /assets/{asset_id}/transcribe-mic` job)
- Modify: `api/src/ai_video_editor/grokbot/voice.py` (`transcript_slice(..., speaker: str | None = None)`)
- Modify: `api/src/ai_video_editor/config.py` (`mic_stream_override: int | None = None`, `mic_min_activity: float = 0.05`)
- Test: `api/tests/test_mic_track.py`, `api/tests/test_transcripts_speaker_migration.py`

**Interfaces:**
- Produces: `AudioStream(index: int, codec: str, channels: int, title: str | None, handler: str | None)`; `probe_audio_streams(path: str) -> list[AudioStream]` (ffprobe `-show_streams -select_streams a`, JSON); `speech_activity(path: str, index: int, *, windows: tuple[float, ...] = (0.1, 0.5, 0.8)) -> float` (fraction of 20 ms frames above an RMS threshold over three 20 s samples at those fractions of the duration); `pick_mic_stream(streams: list[AudioStream], activity: dict[int, float], *, min_activity: float) -> tuple[int | None, str]` returning `(index, method)` with method in `{"label", "activity", "override", "none"}`; `transcribe_mic(asset: dict, duration: float) -> tuple[list[dict], dict]` returning segments and a meta dict `{stream_index, method, activity}` written to `WORKSPACE/mic/<asset_id>.json`; transcripts rows inserted with `speaker='owner'`; `voice.transcript_slice(db_path, asset_id, start, end, speaker=None)` filters `speaker = ?` when given.

- [ ] **Step 1: Tests** in `test_mic_track.py`:
  - `test_pick_by_label`: streams `[a0(title=None), a1(title="Microphone")]` → `(1, "label")` regardless of activity.
  - `test_pick_by_activity_when_unlabelled`: activity `{0: 0.02, 1: 0.31, 2: 0.12}` → `(1, "activity")`.
  - `test_pick_none_when_activity_flat`: `{0: 0.20, 1: 0.18}` → `(None, "none")` (needs ≥ 1.5× the runner-up).
  - `test_pick_none_single_stream`: one stream, activity `0.4` → `(None, "none")`.
  - `test_override_wins`: `settings.mic_stream_override = 3` → `(3, "override")`.
  - `test_extract_audio_maps_stream`: `_extract_audio(path, wav, stream_index=3)` argv contains `"-map", "0:a:3"`.
- [ ] **Step 2: Run → FAIL. Implement** `mic_track.py`; label regex `(?i)mic|microphone|voice|input|commentary` on title or handler_name tag.
- [ ] **Step 3: Migration test** `test_transcripts_speaker_migration.py`: create a DB with the old `transcripts` DDL, run the init/migrate function, assert `PRAGMA table_info` has `speaker`, old rows `speaker IS NULL`, and inserting `speaker='owner'` works. Run → FAIL. Implement the `ALTER TABLE transcripts ADD COLUMN speaker TEXT` guarded by a column check. Run → PASS.
- [ ] **Step 4: Job + slice** `test_transcript_slice_filters_speaker`: two rows at the same time, one `owner`, one NULL → `speaker="owner"` returns only the owner text. Implement `transcribe_mic`: probe, activity, pick, extract with `-map`, run the selected backend's `transcribe_raw` on the WAV, insert rows with `speaker='owner'` after `DELETE ... WHERE video_id=? AND speaker='owner'`; write the meta JSON. Router job `transcribe-mic` mirrors `_run_transcribe_job`. When the pick is None, the job completes with output `"no mic stream (method=none)"` and inserts nothing.
- [ ] **Step 5: Run suite, ruff, secret-scan. Commit** `feat(mic): probe and transcribe the owner's mic stream`.

### Task 3: Commentary bucket, digest section, league scorecard

**Files:**
- Modify: `api/src/ai_video_editor/taxonomy.py:41-48` (`Format.commentary = "commentary"`)
- Create: `api/src/ai_video_editor/commentary.py`
- Modify: `api/src/ai_video_editor/grokbot/digest.py:57-82,191+` (`Digest.commentary: list[CommentaryItem]`, built from owner rows)
- Modify: `api/src/ai_video_editor/grokbot/publish.py` (leak check covers commentary voice)
- Create: `api/src/ai_video_editor/league/scorecard.py`; add `league-scorecard = "ai_video_editor.league.scorecard:main"` to `pyproject.toml [project.scripts]`
- Modify: `.claude/skills/mentor/SKILL.md` (commentary row in the taxonomy table; naming `commentary_<hook>_<date>` is story material, not a short)
- Test: `api/tests/test_commentary.py`, `api/tests/test_digest_commentary.py`, `api/tests/test_league_scorecard.py`

**Interfaces:**
- Consumes: Task 2 owner rows (`speaker='owner'`); `refs.normalize_clip_ref`.
- Produces: `CommentaryItem(ref: str, start_s: float, end_s: float, voice: str, energy: str, attached_beat: str | None)`; `commentary_candidates(owner_rows: list[dict], kept_windows: list[tuple[float, float]], *, limit: int = 5, min_speech_s: float = 4.0) -> list[dict]` (owner rows merged into utterances by a 1.5 s gap, dropped when inside a kept window, ranked by `sentiment_score` desc then length, top `limit`); `GameScore(match_id, champion, kda: str, result: str, duration_s: float, multikill_fights: list[dict], aces: int, owner_multikills: list[dict])`; `game_scorecard(timeline: RiotTimeline, match: dict) -> GameScore`; CLI `league-scorecard [--asset ID]` (default: every cached timeline under `WORKSPACE/riot/`), one line per game sorted by multikill fights desc.

- [ ] **Step 1: Tests** `test_commentary.py`: `test_merges_owner_rows_into_utterances`, `test_drops_utterances_inside_kept_windows`, `test_top_5_by_arousal`, `test_min_speech_4s`. Run → FAIL. Implement `commentary.py`. Run → PASS. Commit `feat(commentary): owner voice utterances as a bucket`.
- [ ] **Step 2: Digest tests** `test_digest_commentary.py`: `test_digest_commentary_section_capped_at_5`, `test_commentary_voice_is_paraphrase_or_energy_plus_beat` (no paraphrase callable → `voice == f"{energy}: {beat}"` or `energy` alone), `test_commentary_leak_check` (a 12-char verbatim fragment in `voice` makes `digest_leaks` non-empty). Implement: `build_digest(..., owner_rows: list[dict] | None = None)` fills `commentary`; `publish` runs `digest_leaks` over commentary too. Commit `feat(digest): commentary section`.
- [ ] **Step 3: Scorecard tests** `test_league_scorecard.py` with an invented timeline: three enemy deaths within 15 s with an owner kill → one multikill fight `{start_s, end_s, enemy_deaths: 3, owner_kills: 1, owner_assists: n}`; five enemy deaths in one fight → `aces == 1`; two owner kills 8 s apart → `owner_multikills == [{t_s, count: 2}]` (10 s rule); `kda == "8/2/13"`, `result in {"WIN","LOSS"}`. Implement `league/scorecard.py` reading `match.json` for champion, KDA and win. CLI prints `match_id  champion  kda  result  mm:ss  multikill_fights  aces  owner_multikills`. Commit `feat(league): per-game multikill scorecard`.
- [ ] **Step 4: Mentor SKILL.md commentary row. Suite, ruff, secret-scan.**

### Task 4: Lane summary from Riot frames

**Files:**
- Create: `api/src/ai_video_editor/league/lane.py`
- Modify: `api/src/ai_video_editor/league/riot_timeline.py` (keep raw `participantFrames` reachable: `parse_lane_frames` reads the cached `timeline.json`, no change to `RiotTimeline`)
- Modify: `api/src/ai_video_editor/grokbot/digest.py` (`Digest.lane: LaneSummary | None`)
- Test: `api/tests/test_lane.py`

**Interfaces:**
- Produces: `LaneMinute(minute: int, owner_cs: int, owner_gold: int, owner_level: int, opp_cs: int, opp_gold: int, opp_level: int)`; `LaneSummary(opponent_champion: str, minutes: list[LaneMinute], cs_diff_at_10: int | None, gold_diff_at_10: int | None, first_blood_s: float | None, first_lane_kill_s: float | None, owner_first_blood: bool)`; `parse_lane_frames(match: dict, timeline: dict, *, owner_puuid: str, offset_s: float, through_minute: int = 14) -> LaneSummary | None` (opponent = enemy participant with the same `teamPosition`; returns None when there is none, e.g. ARAM); raises `ValueError` when a frame's participant id does not map to the champion the match lists; `lane_summary_path(asset_id) -> Path` = `WORKSPACE/riot/<asset_id>/lane.json`.

- [ ] **Step 1: Tests** `test_lane.py`: `test_opponent_is_same_position_enemy`, `test_cs_and_gold_diff_at_10`, `test_first_blood_from_special_kill` (event `CHAMPION_SPECIAL_KILL` with `killType == "KILL_FIRST_BLOOD"`), `test_returns_none_without_lane_opponent`, `test_index_champion_mismatch_raises`, `test_opponent_named_by_champion_only` (no `riotIdGameName`/`summonerName` anywhere in the serialised summary). Run → FAIL. Implement. Run → PASS.
- [ ] **Step 2: Digest** `test_digest_lane_section`: `build_digest(..., lane=LaneSummary(...))` serialises under `lane`. Commit `feat(league): lane summary from Riot frames`.

### Task 5: Story planner and renderer (experiment 1)

**Files:**
- Create: `api/src/ai_video_editor/story/__init__.py`, `story/planner.py`, `story/render.py`, `story/cli.py`; add `story-render = "ai_video_editor.story.cli:main"` to `pyproject.toml`
- Modify: `api/src/ai_video_editor/anthropic_surface.py:76-99` (`check_budget(asset_id, *, cap_usd: float | None = None)`)
- Modify: `api/src/ai_video_editor/vlm/watch_montage.py` (`fps: float = 1.0` parameter; long-form uses 0.25)
- Modify: `api/src/ai_video_editor/config.py` (`story_budget_usd: float = 5.0`, `story_game_audio_volume: float = 0.35`, `story_card_seconds: float = 2.5`)
- Test: `api/tests/test_story_planner.py`, `api/tests/test_story_render.py` (ffmpeg argv captured, no real encode except one marked `real_ffmpeg` test on a 3 s synthetic clip)

**Interfaces:**
- Consumes: `RiotTimeline`, `fights`, `fight_containing` (riot_timeline); `LaneSummary` (Task 4); `mic` meta (Task 2); `render_intro(IntroConfig)` (intros) for cards; `compile._concat(parts, out_path)`; `check_budget`, `record_spend`.
- Produces: `Beat(t_start: float, t_end: float, beat: str, kind: Literal["lane","fight","close","commentary"])`; `Chapter(title: str, start_s: float, end_s: float, kind: str, beats: list[str])`; `StoryPlan(asset_id: str, chapters: list[Chapter], total_s: float, mic_stream: int | None, audio: Literal["mic_over_game","mixed"])`; `plan_story(timeline: RiotTimeline, beats: list[Beat], lane: LaneSummary | None, *, mic_stream: int | None, target_s: float = 330.0, max_s: float = 400.0) -> StoryPlan`; `render_story(asset_path: str, plan: StoryPlan, out_dir: Path, *, cards: bool = True) -> Path` (writes `out_dir/story.json`, `parts/`, `story.mp4`, 1920x1080); `story-render --asset ID --round <panel folder> [--no-cards]`.

- [ ] **Step 1: Planner tests**: `test_chapter_widens_to_fight_rules` (beat 1395-1410 inside a fight 1395.4-1409.9 opening with a kill → chapter `1391.4`-`1412.9`), `test_ally_death_opening_gets_8s` , `test_close_chapter_ends_at_game_end_plus_1`, `test_beats_in_same_fight_merge_into_one_chapter`, `test_lane_chapter_from_first_two_events_before_14min` (two events at 146.8 and 362 s → one lane chapter per event, each ±(4 or 8, 3)), `test_total_trimmed_to_max_by_dropping_lowest_intensity_non_close_chapter`, `test_audio_mixed_when_no_mic`. Run → FAIL. Implement `planner.py` (chronological order; chapter title from the beat text, ≤ 40 chars).
- [ ] **Step 2: Render tests** (argv capture): `test_chapter_filter_mixes_mic_over_game`: argv contains `-filter_complex` with `[0:a:2]volume=1.0[m];[0:a:0]volume=0.35[g];[m][g]amix=inputs=2:duration=first:normalize=0[a]` for `mic_stream=2`, and `scale=1920:1080` ; `test_no_mic_uses_game_audio_only`; `test_timeout_scales_with_chapter_length` (timeout = `60 + 3 * (end - start)`); `test_cards_rendered_between_chapters` (one card per chapter when `cards=True`, via `render_intro` with a text logo of the chapter title, duration `settings.story_card_seconds`); `test_budget_checked_with_story_cap` (`check_budget("story:<asset>", cap_usd=5.0)` called before the critic). Implement `render.py`: per chapter `ffmpeg -ss start -to end -i asset -filter_complex ... -map "[v]" -map "[a]" <codec opts> part_N.mp4`, cards via `render_intro`, concat via `compile._concat`. `real_ffmpeg` test renders two 1.5 s chapters from a generated colour-bars clip with two audio streams and asserts the output duration ≈ 3.0 s (+ cards).
- [ ] **Step 3: CLI** reads `gaming-expert.json` story beats (normalise refs), `WORKSPACE/riot/<asset>/{timeline,match,meta,lane}.json`, `WORKSPACE/mic/<asset>.json`; writes to `WORKSPACE/story/<round>/`. Commit `feat(story): planner, chapter renderer, mic-over-game mix`.
- [ ] **Step 4: Suite, ruff, secret-scan.**

### Task 6: Montage: 9:16_hud layout, ends-only beat snap, music folders with licences, Drive guard

**Files:**
- Modify: `api/src/ai_video_editor/shorts.py:597-620,811-857` (layout table + snap strategy)
- Modify: `api/src/ai_video_editor/music/sync.py` (`strategy="end_on_beat"`)
- Modify: `api/src/ai_video_editor/music/library.py` (read `WORKSPACE/music/royalty_free/` and `WORKSPACE/music/owner_recs/`, each with `licence.json`)
- Modify: `api/src/ai_video_editor/grokbot/publish.py` / `drive.py` upload path (refuse a render whose sidecar says `music_folder == "owner_recs"`)
- Modify: `.gitignore` (`*.m4a *.opus *.flac *.wav`, `**/music/`)
- Test: `api/tests/test_shorts_hud_layout.py`, `api/tests/test_beat_snap_ends.py`, `api/tests/test_music_library_licences.py`, `api/tests/test_publish_music_guard.py`

**Interfaces:**
- Produces: layout key `"9:16_hud"` in `shorts._LAYOUTS` dispatching to `edits.build_hud_aspect_filter_complex` per indexed clip; `snap_clips_to_beats(..., strategy="end_on_beat")` moves `end_seconds` only (±0.5 s); `music.library.Track(path: Path, title: str, source_url: str | None, licence: str, folder: Literal["royalty_free","owner_recs"])`; `list_tracks(folder: str | None = None) -> list[Track]`; `licence.json` schema `{ "<filename>": {"title", "source_url", "licence", "note"} }`; render sidecar `index.json` gains `music_folder`.

- [ ] **Step 1: Layout test**: `_indexed_layout_chain("9:16_hud", 0, game="League of Legends")` returns a chain containing the HUD scoreline and minimap overlays (reuse the existing HUD filter test fixture). Implement by wrapping `build_hud_aspect_filter_complex`. Commit `feat(shorts): 9:16_hud montage layout`.
- [ ] **Step 2: Snap test**: clips with ends 10.0 and 20.3, beats at 10.4 and 20.0 → ends `10.4`, `20.0`, starts unchanged; a clip whose nearest beat is > 0.5 s away is unchanged. `_snap_plan_to_beats` uses `end_on_beat` and runs only when `settings.shorts_beat_sync_enabled` and bucket is a montage. Commit `feat(music): ends-only beat snap for montages`.
- [ ] **Step 3: Library test**: two folders with `licence.json`; `list_tracks()` returns both with `folder` set; a file without a licence entry is skipped with a warning. Guard test: `publish` with a render whose `index.json` has `music_folder: "owner_recs"` → raises/returns `refused: owner_recs music`; `royalty_free` uploads. Commit `feat(music): licensed folders and Drive guard`.
- [ ] **Step 4: .gitignore, suite, ruff, secret-scan.**

### Task 7: HP-bar reader in shadow mode (lowest priority; may go to the bots as spec + tests)

**Files:**
- Create: `api/src/ai_video_editor/cv/__init__.py`, `cv/hp_bar.py`
- Modify: `api/src/ai_video_editor/config.py` (`hp_reader_enabled: bool = False`, `hp_reader_step_s: float = 1.5`)
- Test: `api/tests/test_hp_bar.py` (synthetic 1280x720 frames)

**Interfaces:**
- Produces: `HP_BAR_ROI_720P: dict[str, tuple[int,int,int,int]]` (owner bar `x,y,w,h` under the champion portrait; pinned from a real 720p LoL frame in the test fixture), `read_owner_hp(frame: np.ndarray) -> float | None` (green/red fill fraction; None when the bar region is grey or absent), `scan_hp(video_path: str, *, step_s: float) -> list[tuple[float, float | None]]`, `hp_events(samples, deaths_s: list[float]) -> list[dict]` with `trade` (drop ≥ 0.25 within 6 s then recovery ≥ 0.15 within 20 s) and `escape` (≤ 0.15 then no death within 10 s), written by `scan_hp_shadow(asset_id)` to `WORKSPACE/hp_shadow/<asset_id>.json`; never a candidate while `hp_reader_enabled` is False.

- [ ] **Step 1: Tests**: full bar → `1.0`; half → `0.5` (±0.05); grey bar → None; shielded (white overlay) → fill ignoring white ≤ true value; `hp_events` on a synthetic series yields one trade and one escape; `candidates/service` adds nothing when the flag is off. Run → FAIL. Implement with numpy only (no OpenCV). Commit `feat(cv): HP-bar reader, shadow mode`.

### Task 8: Run (controller, not an implementer)

- [ ] **Step 1:** Deploy the merged tag to the runner checkout; restart only the `dev.exe` tree. `secret-scan`.
- [ ] **Step 2:** Ezreal (asset `35ae633e-9176-4b85-8e65-3757447d02f1`): transcribe → candidates → rank → highlights via `/api/v1`. Mic transcripts (`transcribe-mic`) for Jhin `42672a10…`, Corki `2a8f4ce3…`, Ezreal. Lane summaries and `league-scorecard` for all four Nov 20 games (Zeri asset `0978f6a4…`).
- [ ] **Step 3:** Music: download 4-5 tracks with clear creator licences into `WORKSPACE/music/royalty_free/` with `licence.json` (NCS or CC-BY, credit noted); the owner's six recommendations into `owner_recs/` with `licence.json` notes `label release, local only`.
- [ ] **Step 4:** `story-render --asset 42672a10… --round 2026-11-20_2001_jhin_8-2-13`; critic at 0.25 fps; upload `story.mp4` to Drive `renders/story/2026-11-20_2001_jhin_8-2-13/`.
- [ ] **Step 5:** Montage: multikill fights from the four scorecards → `build_custom_montage(clip_paths, name="montage_nov20_2026-11-22", game="League of Legends", music_path=<royalty_free track>, layout="9:16_hud", max_seconds=90)`; upload to Drive `renders/montage/`.
- [ ] **Step 6:** Republish Jhin and Corki digests (now with commentary and lane sections). Jira AVE-36 comment with links, costs, scorecard, findings; memory update; summary for the owner.
