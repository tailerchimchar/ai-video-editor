# API guide (`api/`)

## What this is

The FastAPI backend (see the root `CLAUDE.md` for the product and workflow).
Local-first and deterministic: source videos are never modified; every output
goes to `WORKSPACE_DIR`.

**Candidate-first:** cheap deterministic signals (below) produce hundreds of
candidate moments; one small LLM call ranks the metadata. The LLM never
watches the whole video.

## Where things live

Look at `src/ai_video_editor/` for the current modules; this file does not list
them (lists go stale). Entry points: `main.py` (app + routers), `config.py`
(every setting), `database.py` (SQLite schema), `routers/` (HTTP), `story/`
(story videos, story-join), `narration/`, `league/` (game understanding),
`grokbot/` (Drive + bots), `decisions/` (Jev), `vlm/`, `registry.py` (the
editing arsenal, generates `.claude/skills/editing-expert/arsenal.md`).

## Candidate sources

| Source | Signal | Cost | Quality |
|---|---|---|---|
| `outplayed_clip` | File ≤ `OUTPLAYED_CLIP_MAX_SECONDS` → it *is* an Outplayed event clip | $0 (one ffprobe) | High (Outplayed already detected it) |
| `riot_api` | League MATCH-V5 timeline `CHAMPION_KILL` events, correlated to the recording's wall-clock window | $0 | Highest (ground truth) |
| `audio_peak` | RMS energy peaks in a low-rate mono WAV | $0 (local ffmpeg+numpy) | Medium (loud ≈ action) |
| `transcript_keyword` | Local Whisper STT (`transcribe` job → `transcripts` table) → spoken hype/funny cues + VADER high-arousal branch | $0 (local CUDA/CPU STT via faster-whisper) | Medium (game-agnostic; cue-based) |
| `cv_kda` | Scoreboard OCR via tesseract per game profile | $0 (local tesseract) | Medium |
| `overwolf_game_event` | **Ground-truth kill/death/assist/spike/clutch from Outplayed's log**; killfeed OCR verifier drops teammate-kill / death frames | $0 | Highest (game-client data) |

## MCP

`mcp/` (FastMCP, stdio) wraps the HTTP endpoints as tools; job tools poll
until done. It needs the API running (`uv run dev`). Never import `mcp` from
`api/` or put tool code here. See `docs/mcp.md`.

### MCP coverage policy: every editing op must be MCP-drivable

A new mutating endpoint owes both halves: the HTTP endpoint in `routers/`
(mutator + render + journal) and a matching tool in
`../mcp/src/ai_video_editor_mcp/server.py` (docstring written as a user manual),
plus a line in `registry.py` (`test_registry` fails otherwise; regenerate
arsenal.md with `uv run python -m ai_video_editor.registry --write`). GET
endpoints get tools when they're part of an iterative workflow. Name tools noun
first with the user's verb (`insert_compilation_intro_after`); `clip_ref` is a
1-based index, UUID prefix or `"M:SS"` everywhere.

## Riot API usage

`candidates/riot.py`: ACCOUNT-V1 (`gameName#tag` → puuid) and MATCH-V5 (match +
timeline). Gotchas:
- **Match pick** uses the Outplayed *filename's* local start time
  (`RECORDING_TIMEZONE`, DST-aware), never `st_ctime`. `_confidence` grades it;
  a low-confidence match may not name folders.
- **Offset per recording** (loading screen length varies): `calibrate.align_offset`
  cross-correlates kills against loudness; weak → `ocr.verify_offset` reads the
  HUD clock. Never a global constant.
- **Status, not silence:** `detect_riot_events` returns `ok|no_match|rate_limited|
  api_error|disabled`, so a throttled key never looks like "no data".
- Dev keys expire every 24 h and only see recent matches. Every Riot error is
  non-fatal (`[]`); other sources still run.

## Cost model

- Local steps (scan, candidate generation incl. Riot/audio, Whisper) cost $0.
- Paid calls are budget-guarded per process (`ANTHROPIC_MAX_RANK_CALLS`,
  `STORY_BUDGET_USD`, `GAME_UNDERSTANDING_BUDGET_USD`, Jev request caps).
- Cheapest first: rules → Jev typed questions (`decisions/`) → Gemini Flash-Lite
  (the play confirmer, `PLAY_CONFIRM_BACKEND`) → Anthropic only where the others
  can't do the job (the ranker, the VLM critic).

## Storage: Supabase vs Google Drive (two jobs, not redundant)

| Store | Holds | Who can read it |
|---|---|---|
| **Supabase Storage** (org `tailer`, project `ai-video-editor`) | Raw VODs the owner **hand-picks** (`outplayed` bucket, same relative path as `OUTPLAYED_MEDIA_DIR`). `workspace` bucket = a one-off 2026-11-23 snapshot, not kept in sync. | The owner and this PC only. |
| **Google Drive** (dedicated bot account, `drive.file` scope) | Clips, digests, previews, renders for the Grok Bot team (`grokbot/drive.py`). | The Grok bots, via their Drive connector. |
| **Local disk** | Everything else (working files, regenerable). | — |

Rules:
- **Nothing uploads to Supabase automatically.** A VOD goes up only when
  the owner asks (`upload_vod_to_cloud` MCP tool → `POST /assets/{id}/cloud`).
  No bulk sync, no scheduled jobs.
- **Bots never get Supabase access.** S3 keys open every bucket, including
  full VODs, which the bot rules say never leave the PC. Bots stay on Drive.
  If that ever changes: a separate bucket + short-lived signed URLs in the
  digest, never keys — and first verify Grok can watch a video from a URL.
- **Budget:** `STORAGE_MAX_TOTAL_GB` (default 100, = Pro's included quota)
  is enforced on every upload; the org spend cap is on as the backstop.
  The quota is org-wide, so the `ConXense` project counts against it too.
- Every upload also writes a `<key>.meta.json` **label** next to the VOD (asset id, file, session, local start time, length, and when the Riot cache exists: champion, K/D/A, result, match id, patch, queue, sync offset; never a puuid or Riot ID). `list_cloud_vods` returns it as `label`; re-running an upload on an unchanged VOD refreshes the label. The sidecar is deleted with the VOD.
- Removing a cloud copy (`remove_vod_from_cloud`) never touches the local file.
- **Cross-machine flow:** PC uploads a pick → another machine pulls it
  (`download_vod_from_cloud` → `POST /cloud/vods/pull`, lands at the same
  relative path, auto-indexed as `source_origin='cloud'`) → normal pipeline
  → **clean up** with `delete_vod_source`, which deletes a `cloud` file only
  while its cloud copy is intact. Never process straight from the cloud:
  the pipeline reads the whole file several times (each = full egress), and
  every stage assumes a local path.
- Keys: `SUPABASE_S3_*` in `.env` only. Supabase Vault doesn't help here
  (reading it needs DB credentials).

### New game flow (VOD → video → Drive)

Every game goes through all of these; a story render alone is not a
finished round. Steps 1–6 and 6c run on the laptop: heavy steps **one at a
time** (~1 GB free with the owner's apps open), each with a memory audit under
`WORKSPACE/audits/memory/<date>_<game>/`. Narration (6b) runs on the PC.

1. VOD local + asset row (pull from Supabase if it came from the PC).
2. `POST /assets/{id}/transcribe` (full audio) and `/transcribe-mic`.
3. `POST /assets/{id}/candidates` (also caches the Riot match/timeline) →
   `/rank` → `/highlights` (cuts clips into `highlights/<game>/<session>`).
4. `league-understand --asset <id>` → `WORKSPACE/plays/<id>.json`.
5. Panel round `WORKSPACE/panel/<YYYY-MM-DD_HHMM_champ_K-D-A>/`: brief →
   gaming/viewer/editing experts in parallel → mentor chairs →
   `clips.json` + `panel-decision.md`.
6. `story-render --asset <id> --round <round> --plays`. What the opening and
   early game keep is owner policy: `knowledge/rules/Early fight start.md` and
   `knowledge/rules/Skip quiet laning, keep heavy trades.md` (the vault wins).
6a. Optional, before narrating: `story_suggest_edits` → owner ticks → `story_apply_suggestions` (Jev, no Anthropic; `docs/story-suggestions.md`).
6b. Narrate in the web page (`/narrate/story/<round>`): record in pieces (record-over
    replaces what it covers), trim/split/nudge/mute/gain takes on the timeline,
    game-audio regions + one duck knob; export → `narrated.mp4`. `docs/narration.md`.
6c. Several games of one series → `story-join` (MCP `story_join`): hook, swipes,
    outro (`--hook-mode play` = montage hook, played through); narrate at
    `/narrate/joined/<stem>`. The owner edits the hook on that PC page (trim,
    extend, move, split, delete; saved to `WORKSPACE/story/<stem>.edits.json`,
    copied to the laptop by hand), then `story-join --apply-edits <stem>`
    re-joins from it (`docs/narration.md`, Hook clip edits). How and why:
    `knowledge/series/Story-join.md`.
7. Owner watches the story; fixes are recorded with `mentor_record_correction`.
8. Drive, only when the owner asks: `grokbot-publish-round <round> --game league --asset-id <id>
   --clips-dir <highlights session>` (clips + digest v3) and the story
   under `renders/story/<round>/`. Publishing emits `round.published`, which
   wakes the Grok bots: while they are frozen, run it with
   `GROKBOT_WEBHOOK_URL=` empty and send the event later.
9. Month pool → monthly montage (royalty-free music only, once the
   precision gate passes).

## Persistence: SQLite now, Postgres later

SQLite (raw SQL via aiosqlite) is the current store — zero-setup,
local-first. The schema is intentionally Postgres-portable: `TEXT` ids
(→ `uuid`), `TEXT` JSON columns (→ `jsonb`), `TEXT` ISO timestamps
(→ `timestamptz`). Migration is a driver + DDL swap, not a model
rewrite. Postgres becomes worthwhile when concurrency or multi-user
arrives.

## Conventions (follow these)

- **Solid, boring design.** Single Responsibility per module: HTTP
  (routers) ≠ orchestration (`candidates/service.py`) ≠ I/O
  (`candidates/*`, `ranker.py`). Don't put ffmpeg or HTTP calls in
  routers.
- **Open/closed for candidate sources.** Add a new source as a new
  `candidates/<name>.py` exposing `detect_*(...) -> list[dict]`, add it
  to `service.compute_candidates`, add the literal to `CandidateSource`.
  Don't touch the ranker or routers.
- **Open/closed for clip windowing.** How a kept suggestion becomes a
  cut window is a per-source strategy in `highlights._STRATEGIES`
  (`riot_api` → exact `metadata.anchor_seconds` ± `HIGHLIGHT_PRE/POST`;
  `outplayed_clip` → whole file; else → LLM-tightened). Any candidate
  carrying `anchor_seconds` gets precise centering for free. Add a
  `_window_*` + registry entry; nothing else changes.
- **Dependency inversion at the seams.** Pure, testable cores
  (`riot._pick_match`, `audio` math) separated from I/O shells. New
  logic should be unit-testable without network/ffmpeg.
- **Fail safe, never fatal for optional enrichment.** Optional sources
  (riot, transcript) return `[]` on any error; the job still succeeds
  with whatever other sources produced.
- **Source files are immutable.** Only read/probe inputs; all writes go
  to `WORKSPACE_DIR`.
- **No `shell=True`, no user-supplied raw args.** ffmpeg commands are
  built from validated Pydantic inputs only.
- **UUID4 ids, ISO-8601 UTC timestamps, everywhere.**
- Lint/format with `ruff` before finishing (`uv run ruff check src/`).
- Secrets live in `.env` (gitignored) — never commit keys.

## Common commands

```bash
uv sync                                   # install (bun: "bun install")
uv run dev                                # run server :8000 (bun: "bun run dev")
uv run ruff check src/ && uv run ruff format src/
```

See `docs/` for architecture deep-dive, API reference, per-source
details, cost/tracing, setup, and the full roadmap.
