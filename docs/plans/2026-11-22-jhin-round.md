# Plan: the Jhin round (spec `docs/specs/spec-jhin-round.md`, Jira AVE-29)

Spec: `docs/specs/spec-jhin-round.md` is the authority; rulings R1-R9 are binding. Jira: AVE-29 (epic), AVE-30..AVE-34 (Tasks 1-5), AVE-35 (Task 6, run by the controller).

## Global Constraints

- Repo `C:\Users\taile\source\repos\ai-video-editor`, Python 3.10+, FastAPI, pydantic v2, httpx only (no SDKs), SQLite. Tests: `cd api && OUTPLAYED_MEDIA_DIR=C:/tmp WORKSPACE_DIR=C:/tmp uv run --no-sync python -m pytest -q`. Lint: `uv run --no-sync ruff check src tests` and `ruff format --check`. Line length 100.
- Never read `api/.env`. Never commit secrets, `.db` files, or anything under `WORKSPACE`. Tests never touch the network (use `httpx.MockTransport` and fakes in `api/tests/conftest.py`).
- Every new behaviour with a cost or a render effect is behind a setting with a safe default; existing paths stay byte-for-byte unchanged when the new inputs are absent.
- Code principles: readable, SRP, DI (pass callables/clients in), extendable = add 1-2 files not edit 5, cost-aware. TDD: red test first.
- Shared names below are contracts; implement them verbatim.
- Report file: write your full report to the path given in the dispatch; return only status, commits, one-line test summary, concerns. Never dispatch subagents.

## Task 1: Riot timeline cache + parser + confidence gate (AVE-30)

**Story:** After correlation, the PC keeps the Riot match and timeline on disk per asset and exposes a typed `RiotTimeline` (all events with champion+side actors, the owner's per-minute gold/CS/level, game end) in recording seconds; a low-confidence match never yields events.

**Files:** new `api/src/ai_video_editor/league/riot_timeline.py`; edit `api/src/ai_video_editor/league/candidates/riot.py` (wire cache + fight metadata + Retry-After); tests `api/tests/league/test_riot_timeline.py` (+ extend `test_riot.py`).

**Contracts (verbatim):**
- `TimelineEvent(kind: Literal["kill","death","assist","ally_death","enemy_death","building","elite","game_end"], t_game_s: float, t_rec_s: float | None, actors: list[str], detail: dict)`; `actor_label(pid: int, participants: dict[int, dict], owner_pid: int) -> str` renders `"you (Jhin)"`, `"ally Thresh"`, `"enemy Diana"` (participants dict: pid → `{championName, teamId}`).
- `OwnerFrame(minute: int, gold: int, cs: int, level: int)`.
- `RiotTimeline(match_id: str, confidence: Literal["high","medium","low"], offset_s: float, game_end_s: float | None, events: list[TimelineEvent], owner_frames: list[OwnerFrame], pre_pad_s: float = 4.0)`.
- `parse_timeline(match: dict, timeline: dict, *, owner_puuid: str, offset_s: float, confidence: str) -> RiotTimeline`: reads `info.frames[].events` for `CHAMPION_KILL` (killer/victim/assistingParticipantIds → kill/death/assist for the owner, ally_death/enemy_death otherwise), `BUILDING_KILL` (detail: buildingType, towerType, laneType), `ELITE_MONSTER_KILL` (detail: monsterType), `GAME_END` (or `info.gameEndTimestamp` − `info.gameStartTimestamp`); `t_rec_s = t_game_s + offset_s`; owner frames from `frames[].participantFrames[<owner pid>]` (`totalGold`, `minionsKilled + jungleMinionsKilled`, `level`).
- `Fight(start_s: float, end_s: float, events: list[TimelineEvent])`; `fights(events: list[TimelineEvent], *, gap_s: float = 15.0) -> list[Fight]` chains `kill/death/assist/ally_death/enemy_death` whose `t_rec_s` is within `gap_s` of the previous included event; other kinds never start or join a fight.
- `load_or_fetch(asset_id: str, *, fetch: Callable[[], tuple[dict, dict, str, str, float] | None], cache_dir: Path | None = None) -> RiotTimeline | None`: cache dir `settings.workspace_dir / "riot" / asset_id`; files `match.json`, `timeline.json`, `meta.json` = `{"match_id","confidence","offset_s","owner_puuid","fetched_at"}`; a cached `confidence == "low"` is never reused (call `fetch` again); `fetch` returns `(match, timeline, match_id, confidence, offset_s)` or None.
- `offset_drift_s(align: Callable[[tuple[float, float]], float]) -> float`: calls `align` on the first and second half of the recording (as `(start_s, end_s)` ranges) and returns the absolute difference; the caller sets `pre_pad_s = 8.0` and logs a warning when > 3.0.
- `riot.py`: when a `RiotTimeline` is available and `confidence != "low"`, each emitted candidate's `metadata` gains `fight_start_s`, `fight_end_s` (from the fight containing the candidate's anchor), `game_end_s` (recording seconds or None) and `match_confidence`; candidates are also emitted for fights with no owner kill? No: only existing candidate kinds, now carrying fight bounds. HTTP helper: on 429, sleep `Retry-After` (cap 10 s) and retry once; second 429 or any 5xx → warning and `[]`.

**Tests (red first):** invented timeline with a kill+assists, an ally death, a nexus BUILDING_KILL, GAME_END, an elite; actor labels; `fights()` chaining and splitting at the gap; `load_or_fetch` round trip; low not reused; Retry-After honoured once (MockTransport); drift > 3 widens `pre_pad_s`; riot candidates carry the four metadata keys; existing `test_riot.py` green.

**Not doing:** other players' frames; window computation (Task 2); digest (Task 3); any change to the Outplayed path.

## Task 2: event-driven windows from fight bounds (AVE-31)

**Story:** A candidate carrying `fight_start_s`/`fight_end_s` gets a window from its fight; everything else is byte-for-byte today's behaviour.

**Files:** `api/src/ai_video_editor/highlights.py`; tests `api/tests/test_fight_window.py` (+ existing highlights tests).

**Contracts (verbatim):** constants `FIGHT_PRE_S = 4.0`, `FIGHT_POST_S = 3.0`, `GAME_END_REACH_S = 25.0`. Pure `fight_window(meta: dict, suggested: tuple[float, float] | None, duration: float, cap: float) -> tuple[float, float]`: `pre = float(meta.get("pre_pad_s", FIGHT_PRE_S))`; `start = fight_start_s - pre`; `end = fight_end_s + FIGHT_POST_S`; if `meta.get("game_end_s") is not None and 0 <= game_end_s - fight_end_s <= GAME_END_REACH_S` then `end = game_end_s + 1.0`; if `suggested`: `start = min(start, suggested[0])`, `end = max(end, suggested[1])`; clamp to `[0.0, duration]`; if `end - start > cap`: `start = end - cap` (front trim). `_window_anchor(cand, r, duration)` delegates to `fight_window` when `cand["metadata"]` has both `fight_start_s` and `fight_end_s`; otherwise unchanged. Cluster midpoint logic is bypassed when fight bounds exist.

**Tests (red first):** pads; reach inside/outside 25 s; ranker widens only; front trim keeps the end; `pre_pad_s` 8.0; the no-bounds path returns exactly what the pre-change function returned (capture expected values from the current implementation for 3 sample candidates before changing it); full suite green.

**Not doing:** candidate generation, digest, ranker.

## Task 3: digest v2 (AVE-32)

**Story:** The digest carries per-clip events, owner game state, a Haiku voice paraphrase and an energy label; the empty-voice bug is fixed; the leak check still refuses transcript text.

**Files:** `api/src/ai_video_editor/grokbot/digest.py`, new `grokbot/voice.py`, `grokbot/publish.py`, `docs/grokbot/queue-format.md`; tests `test_grokbot_digest.py`, `test_grokbot_publish.py`, new `test_grokbot_voice.py`.

**Contracts (verbatim):**
- `DigestEvent(type: str, t_in_clip: float, actors: list[str])`; `GameState(game_time_s: float, gold_start: int | None, gold_end: int | None, cs_start: int | None, cs_end: int | None, level: int | None, game_ended_in_clip: bool)`.
- `DigestClip` gains `events: list[DigestEvent] = []`, `game_state: GameState | None = None`, `energy: Literal["hype_rising","laughter","panic","calm","unknown"] = "unknown"`, `match_confidence: Literal["high","medium","low","none"] = "none"`. `voice: str` stays.
- `build_digest(round_dir: Path, *, game: str, timeline: "RiotTimeline | None" = None, transcript_slices: Callable[[float, float], str] | None = None, paraphrase: Callable[[str], str] | None = None, energy: Callable[[float, float], str] | None = None) -> Digest`. Events = timeline events with `t_rec_s` in `[start_s, end_s]`, `t_in_clip = t_rec_s - start_s`; game_state from the owner frames nearest to the window start and end (`game_time_s = start_s - timeline.offset_s`), `game_ended_in_clip = timeline.game_end_s is not None and start_s <= game_end_s <= end_s`; `match_confidence` from the timeline or `"none"`. Voice: `paraphrase(transcript_slices(start_s, end_s))` when both callables are given and the slice is non-empty; else the gaming-expert `story_beats[0]["beat"]` when `story_beats[0]` is a dict, or the string itself; else `""`.
- `grokbot/voice.py`: `voice_paraphrase(text: str, *, asset_id: str | None = None, client=None) -> str` (one Haiku call via the existing `anthropic_surface` helpers; system rule: "One sentence describing what the player says or reacts to. No quotes, no names, no slurs."; returns `""` on any exception or empty input; feature name `"paraphrase"`); `transcript_slice(db_path: Path, video_id: str, start_s: float, end_s: float) -> str` joins `text` of rows in `transcripts` with `start_seconds < end_s and end_seconds > start_s`; `energy_label(loudness_db: float | None, sentiment: float | None) -> str` (hype_rising if loudness ≥ −20 and sentiment ≥ 0.3; panic if loudness ≥ −20 and sentiment < −0.3; laughter if sentiment ≥ 0.6; calm if loudness is not None and loudness < −30; else unknown).
- `digest_leaks(digest, transcript_path, *, min_len=12)` additionally scans each clip's `voice` with `min_len=8`.
- `publish.py`: when `--asset-id` is given, obtain the timeline via `league.riot_timeline.load_or_fetch(asset_id, fetch=...)` (fetch wired to the existing riot module; None on failure), the transcript slicer bound to `settings.database_path`, and `voice_paraphrase`; `--no-paraphrase` skips Haiku. All injected through `publish_round(..., timeline=None, transcript_slices=None, paraphrase=None)` so tests use fakes.

**Tests (red first):** events clipped; game_state; voice from paraphrase; fallback to beat dict and to string; empty when nothing; energy label table; leak at 8 on voice; publish passes the injected callables (fakes, no network); docs example updated and parses.

**Not doing:** real Haiku calls in tests; other players' stats.

## Task 4: Jev audit log, scorecard, GAME_HINTS_DIR (AVE-33)

**Story:** Every Jev answer is written to a local JSONL log; a command scores Jev against the owner's corrections; corrections live in the workspace.

**Files:** new `api/src/ai_video_editor/decisions/audit.py`, new `decisions/scorecard.py`, edit `decisions/__init__.py`, `config.py`, `vlm/corrections.py`, `vlm/prompts.py`, `api/pyproject.toml` (script `jev-scorecard`), `api/.env.example`; tests `test_decisions_audit.py`, `test_jev_scorecard.py`.

**Contracts (verbatim):**
- `AnswerRecord(ts: str, backend: str, question: str, state_hash: str, state_excerpt: dict, value: Any, probability: float | None, clip_ref: str | None)`; `log_answers(records: list[AnswerRecord], *, log_dir: Path | None = None) -> None` appends one JSON line each to `(log_dir or settings.workspace_dir / "decisions") / f"jev-{YYYY-MM-DD}.jsonl"`; never raises (log a warning instead). `state_excerpt` = the state with keys named `text`, `transcript`, `words`, `voice` removed recursively, serialised and truncated to 600 chars.
- `decisions.ask_many` calls `log_answers` after answering, for every backend, with `backend = select_backend()` (or `"rules"` after fallback); `clip_ref` = `state.get("clip", {}).get("ref")` or `state.get("candidate", {}).get("id")`.
- `scorecard.score(records: list[AnswerRecord], corrections: list[dict]) -> Scorecard(keep_precision: float | None, format_agreement: float | None, disagreements: list[dict], n_records: int, n_corrections: int)`; a correction counts as "not a keeper" when its `tags` contain `"no-event"` or its `rule` contains `"not a highlight"`; format agreement compares `value` for format questions with the confirmed label in corrections whose `rule` names the clip ref and a format word. `main()` prints a short table; script `jev-scorecard = "ai_video_editor.decisions.scorecard:main"`.
- Config: `game_hints_dir: Path | None = None`; `vlm/corrections.py` resolves `settings.game_hints_dir or settings.workspace_dir / "game_hints"` for both reading and writing; the package `vlm/game_hints/` is the read-only seed used only when the workspace file does not exist. `.env.example` gains `GAME_HINTS_DIR` with a comment.

**Tests (red first):** every `ask_many` answer logged (rules backend); excerpt strips text fields and truncates; scorecard on invented records/corrections; corrections written to the workspace dir and read back; seed fallback; full suite green.

**Not doing:** changing Jev questions; uploading the log.

## Task 5: dollar ledger, shorts encoder config, secret-scan (AVE-34)

**Story:** Paid model calls are counted in dollars per asset and refused past the cap; shorts honour the configured encoder; a command refuses to let a key leave the machine.

**Files:** `api/src/ai_video_editor/anthropic_surface.py`, `database.py`, `config.py`, `ranker.py`, `vlm/backends/anthropic_backend.py`, `shorts.py`, new `tools/secret_scan.py`, `grokbot/publish.py` (run the scan on the digest), `api/pyproject.toml` (script `secret-scan`); tests `test_spend_ledger.py`, `test_secret_scan.py`, shorts argv test.

**Contracts (verbatim):**
- `database.py` schema: `spend_ledger(id INTEGER PRIMARY KEY, asset_id TEXT, feature TEXT, model TEXT, input_tokens INTEGER, output_tokens INTEGER, cost_usd REAL, ts TEXT)`.
- `config.py`: `model_prices_usd_per_mtok: dict[str, tuple[float, float]] = {"claude-sonnet-4-6": (3.0, 15.0), "claude-haiku-4-5": (1.0, 5.0)}`; `cost_cap_per_asset_usd: float = 2.0`.
- `anthropic_surface.py`: `record_spend(asset_id: str | None, feature: str, model: str, usage) -> float | None` (usage has `input_tokens`/`output_tokens`; cost from the price table or None; row always written when `asset_id` is set); `check_budget(asset_id: str | None) -> None` raises `SpendCapError` when the summed `cost_usd` for the asset ≥ cap (None asset_id → no check); both take an optional `db_path` for tests.
- Call sites: `ranker.rank_candidates(..., asset_id=None)`, the VLM Anthropic backend, and `grokbot/voice.voice_paraphrase` (Task 3 defines it; if Task 3 is not merged yet, leave a TODO and wire the first two) call `check_budget` before and `record_spend` after each request.
- `shorts.py`: the render argv uses `settings.ffmpeg_video_codec` and `settings.ffmpeg_video_codec_opts.split()` exactly as the compile path does; no literal `veryfast`/`crf` remains in `shorts.py`.
- `tools/secret_scan.py`: `PATTERNS: dict[str, re.Pattern]` for `sk-ant-[A-Za-z0-9_-]{20,}`, `RGAPI-[0-9a-f-]{30,}`, `GOCSPX-[A-Za-z0-9_-]{10,}`, `1//0[A-Za-z0-9_-]{30,}`, `ghp_[A-Za-z0-9]{30,}`, `github_pat_[A-Za-z0-9_]{30,}`, `pk-lf-[a-f0-9-]{20,}`, `sk-lf-[a-f0-9-]{20,}`, `xai-[A-Za-z0-9]{30,}`, and TypeSafe keys (`ts_[A-Za-z0-9]{24,}` plus any 40+ char token on a line containing `TYPESAFE`); `scan_text(text: str) -> list[tuple[str, int]]` (pattern name, line number); `scan_paths(paths: list[Path]) -> list[Hit(path, line, pattern)]` skipping binaries and `.env`; `main()` defaults to `git diff origin/master...HEAD` plus `WORKSPACE/decisions`, `WORKSPACE/riot`, `WORKSPACE/panel/*/digest.json`; prints `path:line pattern` with the value masked; exit 1 on any hit. `grokbot-publish-round` runs `scan_paths([digest.json])` and refuses (exit 6) on a hit.

**Tests (red first):** cost math; unknown model → None cost, row written; cap refusal at the boundary; shorts argv contains the configured opts and nothing hardcoded; every pattern hit on invented strings, values masked; `.env` skipped; publish refuses a digest containing an invented key.

**Not doing:** Jev counting; encoder benchmarking.

## Task 6: run the Jhin round (AVE-35) — controller only

Operational steps live in AVE-35. Not an implementer task.
