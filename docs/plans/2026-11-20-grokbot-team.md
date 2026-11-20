# Grok Bot Team Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give four cloud Grok Bots a safe bridge to this PC: events and media out (webhook + Google Drive), a validated git queue in, a local runner that renders previews and plans, and the four bot skill files.

**Architecture:** Everything leaves the PC by push (`notify/` sinks, `grokbot/drive.py`). Everything comes back through `grokbot/queue.py` (allowlist schemas, applied-ids ledger) consumed by `grokbot/runner.py`, which is the only process acting on bot output and which honors a `SLEEP` flag and emits a heartbeat. `jobs.finish_job` replaces six copied completion helpers and is where `job.done` is emitted. Bot behaviour is markdown in `docs/grokbot/`.

**Tech Stack:** Python 3.12, FastAPI, pydantic v2, httpx (already a dependency; Drive and webhook use it directly, no new SDKs), SQLite via aiosqlite, git via `subprocess` without `shell=True`, pytest.

**Spec:** `docs/specs/spec-grokbot-team.md` (D1–D24). Companion: `docs/specs/spec-expert-panel.md` (D22 privacy).

## Global Constraints

- Config: flat fields on `Settings` in `api/src/ai_video_editor/config.py`, prefixed `grokbot_`, inert when empty, documented in `api/.env.example`. Never read `api/.env` back.
- No `shell=True`; subprocess argv lists only. No user- or bot-supplied string ever becomes an argv element except as a validated file path inside `WORKSPACE_DIR`.
- Privacy (D22, D11): the digest never contains a string that appears verbatim in `transcript.json`, never a summoner name; transcripts, the full VOD and `.env` never leave the PC. Bot skill files carry the "no names, no quotes" rule.
- Queue items are hostile input: `extra="forbid"` on every model, string fields rejected when they contain `http://`, `https://`, a drive-letter path (`X:\`), a POSIX absolute path (`/x/`), backticks, `$(`, `;`, `|`.
- Only a `verdict` whose `quote` is non-empty may reach `append_correction`; everything else goes to `decisions.log`.
- Day one (D24): the runner executes `preview_request`, `sleep`, `wake`, `heartbeat`. `edit_plan`, `verdict`, `deploy` are accepted, validated, ledgered, and NOT executed until Task 8.
- Tests never touch the network, git remotes, Drive, or ffmpeg (fakes/monkeypatch). Full suite: `cd api && uv run python -m pytest -q`. Lint: `uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/` on touched files.
- Skill-budget test (`api/tests/test_skill_budgets.py`) extended to `docs/grokbot/*.md`: 400 lines max.
- Code principles: one responsibility per file; add a sink/kind by adding a file + one registry entry; cost-happy (no polling of paid services; webhook rate cap).

## Review Focus

1. Webhook endpoint down or 5xx: `notify.emit` must never raise or block the job; the job still completes. (Task 1 test `test_webhook_sink_failure_never_raises`.)
2. Runner restart re-reads an already-applied queue item: must be skipped via the ledger, no second render or correction. (Task 5 test `test_ledger_dedupes`, Task 6 test `test_tick_skips_applied_items`.)
3. `preview_request` with a window over 20 s, negative, or past the asset's end: rejected before ffmpeg runs. (Task 5 test `test_preview_window_limits`, Task 6 test `test_preview_beyond_duration_is_rejected`.)
4. `SLEEP` present while items are queued: nothing executes, events are buffered, one `wake.summary` on wake. (Task 6 test `test_sleep_buffers_and_wake_summarizes`.)
5. Digest for a clip missing from an expert JSON: falls back to event name only, never crashes, never reaches into `transcript.json`. (Task 3 tests `test_missing_expert_entry_falls_back`, `test_digest_never_leaks_transcript`.)

---

### Task 1: `notify/` outbound events with a webhook sink

**Files:**
- Create: `api/src/ai_video_editor/notify/__init__.py`, `api/src/ai_video_editor/notify/models.py`, `api/src/ai_video_editor/notify/sinks/__init__.py`, `api/src/ai_video_editor/notify/sinks/log.py`, `api/src/ai_video_editor/notify/sinks/webhook.py`
- Modify: `api/src/ai_video_editor/config.py` (after the `typesafe_*` block), `api/.env.example`
- Test: `api/tests/test_notify.py`

**Interfaces:**
- Produces: `notify.models.Event(BaseModel)`: `event: str`, `payload: dict = {}`, `ts: str` (ISO UTC, default now). `notify.emit(event: Event) -> None` (sync, never raises, fans out to every configured sink). `notify.sinks.Sink` Protocol: `send(event: Event) -> None`. `notify.sinks.SINKS: dict[str, Callable[[], Sink | None]]` factories returning `None` when unconfigured. `notify.sinks.webhook.WebhookSink(url: str, key: str, *, max_per_hour: int, client: httpx.Client | None = None, clock: Callable[[], float] = time.monotonic)` with `send`; `notify.sinks.log.LogSink()` logs at INFO.
- Config: `grokbot_webhook_url: str = ""`, `grokbot_webhook_key: str = ""`, `grokbot_max_events_per_hour: int = 120`, `grokbot_timeout_seconds: float = 10.0`.

- [ ] **Step 1: Write the failing tests** in `api/tests/test_notify.py`

```python
def test_webhook_sink_posts_bearer_json(): # httpx.MockTransport captures url, Authorization "Bearer k", body == event.model_dump()
def test_webhook_sink_failure_never_raises(): # transport raises httpx.ConnectError -> send() returns None, no exception
def test_webhook_sink_rate_cap_folds_extra_events(): # max_per_hour=2, three sends in one hour -> 2 POSTs; the third is dropped and sink.dropped == 1
def test_emit_fans_out_to_every_configured_sink(monkeypatch): # SINKS patched with two fakes -> both receive the event
def test_emit_with_no_sinks_is_a_noop(): # settings.grokbot_webhook_url "" -> emit() returns None, no error
def test_sink_registry_adds_webhook_only_when_url_and_key_set(monkeypatch)
```

- [ ] **Step 2: Run** `cd api && uv run python -m pytest tests/test_notify.py -q` → FAIL (module not found)
- [ ] **Step 3: Implement** the files above. `emit` catches every exception per sink and logs a warning. The rate cap is a sliding one-hour window of send timestamps; dropped events increment `WebhookSink.dropped` (read by the heartbeat in Task 6).
- [ ] **Step 4: Run** the test file → PASS; run ruff on touched files.
- [ ] **Step 5: Commit** `feat(notify): outbound events with webhook + log sinks`

### Task 2: one `finish_job` helper that emits `job.done`

**Files:**
- Create: `api/src/ai_video_editor/jobs.py`
- Modify: `api/src/ai_video_editor/routers/analyze.py:50-59` (`_finish`), `routers/assets.py:439-448`, `routers/shorts.py:57-67`, `routers/edits.py:107-114`, `routers/league.py:39-46`, `routers/sfx.py:41-48` — delete the local helper, import `finish_job`, keep call sites' keyword style; `routers/analyze.py:270-272` and `:345-347` and `routers/shorts.py:155-161,296-303,464-471,621-628` pass `result=summary`/`result=result` and `job_type=`.
- Test: `api/tests/test_jobs.py`

**Interfaces:**
- Produces: `jobs.finish_job(db, job_id: str, *, output_path: str | None = None, error: str | None = None, job_type: str | None = None, result: dict | None = None) -> None`. Writes the same `UPDATE jobs ...` row as today (best-effort, suppressed), then calls `notify.emit(Event(event="job.done", payload={"job_id", "job_type", "status", "output_path", "error", "summary"}))` where `summary` is `jobs.summarize(result)`.
- `jobs.summarize(result: dict | None) -> dict`: keeps only keys in `("folder", "clips_written", "clips_total", "shorts_written", "shorts_total", "dropped", "game", "champion")` plus `clips: [{"file","event","start_seconds","end_seconds","vlm_verdict"}]` / `shorts: [{"file","bucket","title","vlm_verdict"}]` when present. Never includes `reason`, `vo_prompt`, transcripts.

- [ ] **Step 1: Write the failing tests** in `api/tests/test_jobs.py`
```python
async def test_finish_job_updates_row_and_emits(monkeypatch): # in-memory db row -> status completed, completed_at set; captured Event has event "job.done", payload["job_type"]=="highlights", payload["summary"]["clips_written"]==3
async def test_finish_job_error_path_emits_failed(monkeypatch)
def test_summarize_strips_free_text(): # result with "reason" and "vo_prompt" -> absent in summary
async def test_finish_job_survives_db_failure(monkeypatch): # db.execute raises -> no exception, event still emitted with status "completed"
def test_no_router_defines_a_private_finish_helper(): # grep routers/ source for "async def _finish" -> none
```
- [ ] **Step 2: Run** → FAIL
- [ ] **Step 3: Implement** `jobs.py`; replace the six helpers; run the whole suite (job tests in `tests/test_*router*.py` must stay green).
- [ ] **Step 4: Run** `uv run python -m pytest -q` → all pass
- [ ] **Step 5: Commit** `refactor(jobs): single finish_job emitting job.done`

### Task 3: digest builder (pure, leak-tested)

**Files:**
- Create: `api/src/ai_video_editor/grokbot/__init__.py`, `api/src/ai_video_editor/grokbot/digest.py`
- Test: `api/tests/test_grokbot_digest.py`, fixture dir `api/tests/fixtures/panel_round/` (copy the shape of `WORKSPACE/panel/2026-11-19_zeri/` with INVENTED content: `clips.json` (3 clips, keys as the real file: index, file, event, event_type, start_seconds, end_seconds, anchor_seconds, hype_score, funny_score, story_score, reason, ok, skip_reason, vlm_verdict, vlm_reason), `gaming-expert.json` (list of {clip_ref, quality, quality_reason, concepts, story_beats, talking_points, unsure}; only 2 of the 3 clips present), `viewer-expert.json` (dict with a `clips` list of {clip_ref, hook_verdict, hook_reason, copy{title,caption,hashtags}, thumbnail_t, would_keep_watching}), `transcript.json` (list of {start_seconds, end_seconds, text} with one line that also appears in `gaming-expert.json.quality_reason` to prove the leak test bites), `panel-decision.md`).

**Interfaces:**
- Produces: `DigestClip(BaseModel)`: `ref: str` (`"12m34s"` form from `clips.json` `file` stem, e.g. `01_kill_14m32s.mp4` → `14m32s`), `event: str`, `start_s: float`, `end_s: float`, `panel: dict` (`gaming_quality: int | None`, `hook_verdict: str | None`, `format: str | None`), `critic: str | None` (`vlm_verdict`), `voice: str` (first `story_beats` entry from gaming-expert, else `""`), `copy: dict | None` (viewer copy), `drive: str | None = None`. `Digest(BaseModel)`: `game: str`, `round: str` (folder name), `clips: list[DigestClip]`, `built_at: str`. `build_digest(round_dir: Path, *, game: str) -> Digest`. `digest_leaks(digest: Digest, transcript_path: Path, *, min_len: int = 12) -> list[str]` returns transcript substrings (≥ min_len chars) found verbatim in `digest.model_dump_json()`. `annotate_with_jev(digest: Digest, ask_many=decisions.ask_many) -> Digest` adds `panel["format"]` and `panel["include_p"]` using `bucketing._FORMAT_QUESTION` / `_INCLUDE_QUESTION` with state `{"clip": clip.model_dump()}`.
- `build_digest` never opens `transcript.json`.

- [ ] **Step 1: Write the failing tests**
```python
def test_build_digest_reads_clips_and_experts(fixture_round): # 3 clips; clip[0].ref == "14m32s"; panel["gaming_quality"]==4; panel["hook_verdict"]=="pass"; critic=="ship_it"; voice startswith story_beats[0]
def test_missing_expert_entry_falls_back(fixture_round): # third clip: panel gaming_quality None, voice ""
def test_digest_never_leaks_transcript(fixture_round, monkeypatch): # build_digest with voice replaced by a transcript line -> digest_leaks returns that line; the real digest -> []
def test_build_digest_never_opens_transcript(fixture_round, monkeypatch): # monkeypatch Path.open/read_text to fail on 'transcript.json'
def test_annotate_with_jev_uses_ask_many(): # fake ask_many returns Answer(Format.funny, .8) and Answer(True, .9) -> panel format "funny", include_p 0.9
```
- [ ] **Step 2: Run** → FAIL
- [ ] **Step 3: Implement** `digest.py`
- [ ] **Step 4: Run** → PASS, ruff
- [ ] **Step 5: Commit** `feat(grokbot): names-free panel digest with leak test`

### Task 4: Google Drive uploader (httpx, OAuth refresh token) + auth helper

**Files:**
- Create: `api/src/ai_video_editor/grokbot/drive.py`, `api/src/ai_video_editor/grokbot/drive_auth.py`
- Modify: `api/src/ai_video_editor/config.py`, `api/.env.example`, `api/pyproject.toml` (`[project.scripts] drive-auth = "ai_video_editor.grokbot.drive_auth:main"`)
- Test: `api/tests/test_grokbot_drive.py`

**Interfaces:**
- Config: `grokbot_drive_client_id: str = ""`, `grokbot_drive_client_secret: str = ""`, `grokbot_drive_refresh_token: str = ""`, `grokbot_drive_folder_id: str = ""`, `grokbot_drive_preview_retention_days: int = 7`.
- Produces: `Uploader` Protocol: `upload(path: Path, *, name: str, subfolder: str) -> str` (returns Drive file id), `prune(*, subfolder: str, older_than_days: int) -> int`. `DriveUploader(client_id, client_secret, refresh_token, folder_id, *, client: httpx.Client | None = None, now: Callable[[], datetime] = ...)` implements it against `https://oauth2.googleapis.com/token` (refresh grant) and `https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart` / `https://www.googleapis.com/drive/v3/files` (list with `q`, delete). Subfolders (`clips`, `renders`, `previews`, `digests`) are created on first use under `folder_id` and cached. `DriveError(RuntimeError)` on non-2xx. `uploader_from_settings() -> Uploader | None` (None when any field empty). `drive_auth.main()` runs the installed-app loopback OAuth flow on `http://127.0.0.1:8765/` with scope `https://www.googleapis.com/auth/drive.file` and prints the refresh token once with the instruction to paste it into `api/.env` as `GROKBOT_DRIVE_REFRESH_TOKEN`.

- [ ] **Step 1: Write the failing tests**
```python
def test_upload_refreshes_token_then_posts_multipart(tmp_path): # MockTransport: first call token endpoint -> access_token; then upload -> returns {"id": "f1"}; assert Authorization Bearer, parent folder id in metadata, returned "f1"
def test_access_token_is_cached_until_expiry()
def test_subfolder_created_once_and_reused()
def test_prune_deletes_only_older_files(): # list returns 2 files, one 8 days old -> one DELETE
def test_non_2xx_raises_drive_error()
def test_uploader_from_settings_none_when_unconfigured(monkeypatch)
```
- [ ] **Step 2: Run** → FAIL
- [ ] **Step 3: Implement** `drive.py` and `drive_auth.py` (stdlib `http.server` for the loopback; `webbrowser.open`; exchange code with httpx).
- [ ] **Step 4: Run** → PASS, ruff
- [ ] **Step 5: Commit** `feat(grokbot): Drive uploader + one-time auth helper`

### Task 5: queue models, hostile-input validator, applied ledger

**Files:**
- Create: `api/src/ai_video_editor/grokbot/queue.py`
- Modify: `api/src/ai_video_editor/database.py` (schema: `CREATE TABLE IF NOT EXISTS queue_applied (id TEXT PRIMARY KEY, kind TEXT NOT NULL, applied_at TEXT NOT NULL, outcome TEXT NOT NULL)`)
- Test: `api/tests/test_grokbot_queue.py`

**Interfaces:**
- Produces (all `model_config = ConfigDict(extra="forbid")`): `Envelope`: `id: str` (uuid4 string), `kind: Literal[...]`, `author_bot: Literal["lead","editor","devops"]`, `created: str`, `schema_version: Literal[1]`. Items: `EditPlanItem(kind="edit_plan", compilation_id: str, base_version: str, plan: EditPlan, copy: CopyBlock | None)` with `CopyBlock(title: str, caption: str, hashtags: list[str], thumbnail_t: float | None)`; `PreviewRequestItem(kind="preview_request", asset_id: str | None, compilation_id: str | None, start_s: float, end_s: float, question: str)` (exactly one of asset_id/compilation_id; `0 <= start_s < end_s`, `end_s - start_s <= 20`); `VerdictItem(kind="verdict", game: str, clip_ref: str, verdict: Literal["payoff","false_positive","style"], rule: str, quote: str = "", quote_ts: str | None)`; `DeployItem(kind="deploy", tag: str)` (`^v\d+\.\d+\.\d+$`); `SleepItem(kind="sleep")`, `WakeItem(kind="wake")`. `QueueItem = Annotated[Union[...], Field(discriminator="kind")]`.
- `class QueueRejected(ValueError)`: `reason: str`. `parse_item(text: str) -> QueueItem` raises `QueueRejected` on JSON error, validation error, hostile strings (walk every `str` in `model_dump()`; pattern per Global Constraints), or an `EditPlanItem` whose `plan.effects` has any `at != 0.0` (reason `"timed effects are not applied yet"`) or whose `plan.cards`/`plan.sfx` is non-empty (preflight refuses them today).
- `class AppliedLedger`: `__init__(self, db_path: Path)` (sync `sqlite3`, runner runs outside the API process), `seen(id) -> bool`, `mark(id, kind, outcome: str) -> None`, `outcomes() -> dict[str, str]`.
- `parse_sleep_flag(repo_root: Path) -> bool` (`SLEEP` file exists at the repo root).

- [ ] **Step 1: Write the failing tests**
```python
def test_parse_preview_request_ok()
def test_extra_field_is_rejected(): # unknown key "slowmo" -> QueueRejected mentioning "extra"
def test_hostile_strings_rejected(): # question "see https://x" ; rule "C:\\x" ; clip_ref "a;b" -> each QueueRejected
def test_preview_window_limits(): # end-start 25 -> rejected; start 5 end 3 -> rejected; start -1 -> rejected
def test_edit_plan_with_timed_effect_rejected()
def test_edit_plan_with_cards_or_sfx_rejected()
def test_deploy_tag_format()
def test_ledger_dedupes(tmp_path): # mark then seen True; second mark same id is a no-op
def test_discriminator_unknown_kind_rejected()
```
- [ ] **Step 2: Run** → FAIL
- [ ] **Step 3: Implement** `queue.py`; add the table to `database.py` SCHEMA (keep the table creation idempotent; `AppliedLedger` creates it too for the standalone runner).
- [ ] **Step 4: Run** → PASS, ruff
- [ ] **Step 5: Commit** `feat(grokbot): queue item schemas, hostile-input validator, applied ledger`

### Task 6: the runner (previews, sleep/wake, heartbeat, deferred kinds)

**Files:**
- Create: `api/src/ai_video_editor/grokbot/runner.py`, `api/src/ai_video_editor/grokbot/git_queue.py`
- Modify: `api/src/ai_video_editor/config.py` (`grokbot_queue_repo_dir: Path | None = None`, `grokbot_runner_poll_seconds: int = 60`, `grokbot_heartbeat_seconds: int = 300`, `grokbot_queue_depth_cap: int = 10`), `api/.env.example`, `api/pyproject.toml` (`grokbot-runner = "ai_video_editor.grokbot.runner:main"`)
- Test: `api/tests/test_grokbot_runner.py`

**Interfaces:**
- Consumes: Task 1 `notify.emit`, Task 3 `build_digest` (not called by the runner; the digest is built by the chair session), Task 4 `Uploader`, Task 5 `parse_item`, `AppliedLedger`, `QueueItem` classes, `editing.trim_clip(asset_path, output_path, start_seconds, end_seconds) -> (ok, err)`.
- Produces: `QueueRepo` Protocol: `pull() -> None`, `list_items() -> list[tuple[str, str]]` (relative path, text) for `queue/*.json`, `write(relative_path: str, text: str) -> None`, `commit_push(message: str) -> None`, `root: Path`. `GitQueueRepo(root: Path, *, git: str = "git", run=subprocess.run)` implements it with argv lists (`["git","-C",str(root),"pull","--ff-only"]`, `add`, `commit -m`, `push`); never raises on a failed push (logs, retries next tick).
- `ApiClient` Protocol: `asset_path(asset_id: str) -> tuple[str, float]` (path, duration seconds; via `GET /api/v1/assets/{id}`), `compilation_output(compilation_id) -> str | None`. `HttpApiClient(base_url: str, client: httpx.Client | None = None)`.
- `class RunnerState(BaseModel)`: `sleeping: bool = False`, `buffered: list[dict] = []`, `last_heartbeat: float = 0.0`, `version: str` (from `importlib.metadata` or `"dev"`).
- `class Runner`: `__init__(self, *, repo: QueueRepo, api: ApiClient, uploader: Uploader | None, ledger: AppliedLedger, emit=notify.emit, trim=editing.trim_clip, previews_dir: Path, clock=time.monotonic)`. `tick() -> list[str]` (returns outcomes for the tick): `repo.pull()`; `sleeping = parse_sleep_flag(repo.root)`; for each new item (not `ledger.seen`): parse → on `QueueRejected` write `rejected/<file>.json` `{"id","reason"}` + emit `queue.rejected` + `ledger.mark(id,"?", "rejected")`; `SleepItem`/`WakeItem` set/clear the `SLEEP` file via `repo.write` and on wake emit one `wake.summary` with the buffered events then clear the buffer; while sleeping every other item stays unapplied (not marked) and emits are buffered instead of sent; `PreviewRequestItem` → `_preview(item)`: resolve path + duration via `api.asset_path`, reject when `end_s > duration`, `trim` into `previews_dir / f"{item.id}.mp4"`, upload to subfolder `previews` when an uploader exists, emit `preview.ready` `{"request_id", "drive": {"mp4": file_id | None}, "question", "local_path"}`, mark `"applied"`; `EditPlanItem`, `VerdictItem`, `DeployItem` → mark `"deferred"` and emit `queue.deferred` `{"id","kind"}` (Task 8 replaces this); queue depth over `grokbot_queue_depth_cap` → emit `queue.depth` once per tick. `heartbeat_due() -> bool`; `heartbeat() -> None` emits `{"event":"heartbeat","payload":{"runner_version","queue_depth","sleep","dropped_events"}}`. `commit_push("runner: <n> outcomes")` only when something was written.
- `main()`: loop `tick()`, `heartbeat()` when due, sleep `grokbot_runner_poll_seconds`; exits non-zero when `grokbot_queue_repo_dir` is unset.

- [ ] **Step 1: Write the failing tests** (fake repo with an in-memory file dict, fake api, fake uploader, captured emits, fake trim writing an empty file)
```python
def test_tick_renders_preview_uploads_and_emits()
def test_tick_skips_applied_items()
def test_preview_beyond_duration_is_rejected(): # asset duration 30, end_s 35 -> rejected/<file>.json written, queue.rejected emitted, nothing trimmed
def test_malformed_item_is_rejected_with_reason()
def test_sleep_buffers_and_wake_summarizes(): # SLEEP written by a sleep item; a preview item in the same tick is not applied and no preview.ready is sent; a wake item -> one wake.summary carrying the buffered events, then the preview renders on the next tick
def test_deferred_kinds_are_ledgered_not_executed(): # edit_plan, verdict, deploy -> outcome "deferred", no api calls
def test_heartbeat_payload_and_cadence()
def test_queue_depth_cap_emits_once()
def test_git_queue_repo_uses_argv_lists(): # fake run records argv; no string commands; failed push does not raise
```
- [ ] **Step 2: Run** → FAIL
- [ ] **Step 3: Implement** `runner.py`, `git_queue.py`
- [ ] **Step 4: Run** → PASS, ruff; full suite green
- [ ] **Step 5: Commit** `feat(grokbot): queue runner with previews, sleep/wake, heartbeat`

### Task 7: bot skill files and setup guide

**Files:**
- Create: `docs/grokbot/lead.md`, `docs/grokbot/editor.md`, `docs/grokbot/devops.md`, `docs/grokbot/viewer.md`, `docs/grokbot/setup.md`, `docs/grokbot/queue-format.md`
- Modify: `api/tests/test_skill_budgets.py` (add `docs/grokbot/*.md` to the 400-line cap and a test that each bot file contains the strings `never quote another player`, `never include summoner names`, `default` (interrupt rule), `sleep`), `README.md` (one paragraph + link)
- Test: `api/tests/test_skill_budgets.py`

**Interfaces:**
- Consumes: Task 5 item schemas (examples in `queue-format.md` must parse with `parse_item`; add `test_queue_format_examples_parse` that extracts every ```json block from `docs/grokbot/queue-format.md` and asserts `parse_item` accepts it).
- Content each bot file must carry (from the spec §3–§4, §7–§10): role and seat; who it reports to; wake rule (Lead always on; others on assignment from the Lead; Viewer one creator study a day); the interrupt rules (text freely, every question carries the default it will proceed with, late replies are corrections; the Lead's evening digest includes usage consumed and remaining); the daily turn cap (Lead 200 turns, Editor 60, DevOps 120, Viewer 60 — initial values, re-decided after day one); the no-polling rule (never check git or Drive in a loop; act on webhook events and assignments); `sleep`/`wake` handling; privacy rules verbatim; what it may write where (Lead/Editor: queue repo items only; DevOps/Viewer: branches + PRs on the main repo, never master; Viewer: no queue access, hands hook verdicts and copy to the Lead); the PR ping format (one paragraph, risk level low/high, files touched count); content direction line (philosophical, funny, wide variety, montage-y, heartfelt with story); the creators to study (zekken, jawgemo, ryuk lol, MrBeast) and the `viewer-expert/log.md` entry format `- YYYY-MM-DD analytics: <platform> <video> <summary>. Lesson: <...>`; trend watch only after asking Noodlz each time.
- `setup.md`: ordered checklist Noodlz follows: Grok Bot trial; create the four bots and paste each skill; Lead: add inbound webhook routine → copy POST URL + key into `GROKBOT_WEBHOOK_URL` / `GROKBOT_WEBHOOK_KEY`; Lead: add Atlassian remote MCP (`https://mcp.atlassian.com/v1/sse`) signed in as the bot Atlassian user; new Google account → Google Cloud project with Drive API + OAuth desktop client → `uv run drive-auth` → `GROKBOT_DRIVE_*`; new private GitHub repo `ai-video-editor-queue` with `queue/`, `rejected/` folders and a README → fine-grained token (contents write) for Lead+Editor; fine-grained token (contents + pull requests) for DevOps+Viewer on the main repo; clone the queue repo on the PC → `GROKBOT_QUEUE_REPO_DIR`; Task Scheduler command `uv run --project C:\...\api grokbot-runner` with restart on failure; PC sleep off during work hours; Drive connector in the bots signed into the dedicated account.

- [ ] **Step 1: Write the failing tests** (budget + required strings + queue examples parse)
- [ ] **Step 2: Run** → FAIL
- [ ] **Step 3: Write** the six markdown files (each ≤ 400 lines; skills ≤ 200 is the target)
- [ ] **Step 4: Run** → PASS
- [ ] **Step 5: Commit** `docs(grokbot): bot skills, queue format, setup guide`

### Task 8: edit-plan execution and the verdict path (phase 2, built behind a flag)

**Files:**
- Modify: `api/src/ai_video_editor/grokbot/runner.py` (replace the deferred branches for `EditPlanItem` and `VerdictItem`), `api/src/ai_video_editor/routers/plan.py` (add `GET /compilations/{compilation_id}/plan/version` → `{"version": str}` using `EditsPlanExecutor.current_version`), `api/src/ai_video_editor/config.py` (`grokbot_execute_plans: bool = False`, `grokbot_record_verdicts: bool = False`)
- Test: `api/tests/test_grokbot_runner_phase2.py`, `api/tests/test_plan_router.py` (version endpoint)

**Interfaces:**
- Consumes: `POST /api/v1/compilations/{id}/plan` (422 problems / 409 unsupported / 200 `PlanResponse`), `vlm.watch_montage.watch_montage(video_path) -> WatchResult` (`critique.overall_verdict`, `opener_strength`, `climax_strength`, `pacing`), `POST /api/v1/mentor/record_correction` body `{game, feedback, rule, clip_ref, source_context, tags, verdict}`.
- `ApiClient` gains: `plan_version(compilation_id) -> str`, `apply_plan(compilation_id, plan: dict) -> dict` (raises `ApiError(status, detail)`), `record_correction(body: dict) -> None`.
- Runner: `EditPlanItem` when `grokbot_execute_plans`: `api.plan_version` ≠ `item.base_version` → reject with reason `f"base_version {item.base_version} != current {current}"` and payload `current` (the version string) ; else `api.apply_plan` → on 4xx reject with the API detail → on 200 resolve `api.compilation_output` → `watch(output)` (injected, default `watch_montage`) → upload `renders/` + write `critique.json` next to it and upload `digests/` → emit `render.done` `{"plan_id","drive":{"mp4","critique"},"critic":{"verdict","opener","climax","pacing"},"copy": item.copy}` → mark `"applied"`. When the flag is off: previous `"deferred"` behaviour.
- `VerdictItem` when `grokbot_record_verdicts`: `quote` non-empty → `api.record_correction({game, feedback: quote, rule, clip_ref, source_context: f"grokbot:{item.id}", tags: ["grokbot"], verdict})` → mark `"applied"`; empty quote → append one JSON line to `previews_dir.parent / "decisions.log"` and mark `"logged"`. Flag off → `"deferred"`.

- [ ] **Step 1: Write the failing tests**
```python
def test_stale_plan_is_rejected_with_current_version()
def test_plan_applied_rendered_critiqued_uploaded_and_reported()
def test_plan_api_422_becomes_rejection_with_detail()
def test_verdict_with_quote_reaches_mentor_once(): # second tick, same id -> no second record_correction
def test_verdict_without_quote_goes_to_decisions_log_only()
def test_flags_off_keep_items_deferred()
async def test_plan_version_endpoint(client)
```
- [ ] **Step 2: Run** → FAIL
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run** → PASS; full suite green; ruff
- [ ] **Step 5: Commit** `feat(grokbot): plan execution + quoted-verdict path behind flags`
