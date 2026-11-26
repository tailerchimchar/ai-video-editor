# CLAUDE.md

Guidance for Claude (and humans) working in this repository.

## What this is

A **local-first, deterministic video automation API** that turns raw
Outplayed gameplay recordings into ranked highlight suggestions and,
eventually, edited highlight reels — without uploading source footage
anywhere.

It is a FastAPI backend. Source videos are **never modified**; every
output goes to a separate workspace directory.

## The problem we're solving

A gamer accumulates hundreds of hours of Outplayed recordings (full
session VODs + Outplayed's auto-cut event clips). Finding the good
moments by hand is infeasible, and "ask an AI to watch the video" is
slow and expensive (vision tokens over hours of footage).

**Core insight — the candidate-first architecture:** generate ~hundreds
of *cheap* candidate moments from deterministic signals, then use one
small LLM call to *reduce and rank* them. The LLM never watches video;
it ranks structured metadata. This is ~100× cheaper than vision and
fully traceable.

```
recordings ──> candidate generators (cheap, deterministic, no LLM)
                 ├─ outplayed_clip   (Outplayed already cut it)
                 ├─ riot_api         (exact kill/death timestamps)
                 ├─ audio_peak       (loud regions in full VODs)
                 └─ transcript_keyword (stub — needs STT)
                          │
                          ▼
              HighlightCandidate rows (one table, source-tagged)
                          │
                          ▼
              LLM ranker (1 call/video) ──> keep + funny/hype/story
                          │                  scores + reason
                          ▼
              ranked suggestions  (analyzer-only; no clips cut yet)
```

## Architecture map

| Path | Responsibility |
|---|---|
| `src/ai_video_editor/main.py` | FastAPI app factory, lifespan, router wiring |
| `config.py` | All settings via `pydantic-settings` (env / `.env`) |
| `database.py` | SQLite (aiosqlite, raw SQL), schema, migrations |
| `models.py` | Pydantic request/response + `HighlightCandidate` |
| `tracing.py` | Langfuse init (AnthropicInstrumentor) + flush |
| `ranker.py` | LLM candidate ranker (Anthropic SDK) + spend guard |
| `editing.py` | Shared ffmpeg `trim_clip` (used by clips + highlights) |
| `edits.py` | Phase-3 editing primitives: `apply_zoom`/`apply_caption`/`apply_focus` |
| `compile.py` | Phase-3 compile pipeline: per-clip render → concat → optional music mix |
| `highlights.py` | Cut kept ranked suggestions into an organized folder |
| `candidates/` | Candidate generators (one module per source) |
| `candidates/service.py` | Orchestrates all sources for one asset |
| `storage.py` | Supabase Storage (S3 API) client for hand-picked raw-VOD backups |
| `routers/cloud.py` | Upload / list / remove a VOD's cloud copy |
| `routers/` | HTTP layer: assets, clips, projects, jobs, analyze |

### Phase 1 (done) — deterministic editing primitives
Scan assets → trim clips (ffmpeg) → projects/timeline → render rough
cut. All ffmpeg work runs in background threads tracked via a `jobs`
table; poll `GET /api/v1/jobs/{id}`.

**Batch clips (no LLM):** `POST /clips/batch-highlights?game=` pools a
game's short Outplayed clips into `highlights/<game>/clips_<date>/`
without ranking — Outplayed already curated them and the candidate has
no rankable signal, so the LLM is deliberately skipped ($0). Use for
Valorant (no Riot) and clip backlogs.

### Phase 2 (done) — the analyzer
Candidate generation + LLM ranking. Returns ranked suggestions; review
them, then `POST /assets/{id}/highlights` cuts every kept suggestion
into `WORKSPACE/highlights/<game>/<date>_<champion|HHhMM>/` with
descriptive names + `index.md`/`index.json`. Champion comes from Riot
data; the folder name is a pure function of the asset + candidates
(`highlights.relative_folder`) so POST/GET agree without storing a path.

## Candidate sources

| Source | Signal | Cost | Quality |
|---|---|---|---|
| `outplayed_clip` | File ≤ `OUTPLAYED_CLIP_MAX_SECONDS` → it *is* an Outplayed event clip | $0 (one ffprobe) | High (Outplayed already detected it) |
| `riot_api` | League MATCH-V5 timeline `CHAMPION_KILL` events, correlated to the recording's wall-clock window | $0 | Highest (ground truth) |
| `audio_peak` | RMS energy peaks in a low-rate mono WAV | $0 (local ffmpeg+numpy) | Medium (loud ≈ action) |
| `transcript_keyword` | Local Whisper STT (`transcribe` job → `transcripts` table) → spoken hype/funny cues + VADER high-arousal branch | $0 (local CUDA/CPU STT via faster-whisper) | Medium (game-agnostic; cue-based) |
| `cv_kda` | Scoreboard OCR via tesseract per game profile | $0 (local tesseract) | Medium |
| `overwolf_game_event` | **Ground-truth kill/death/assist/spike/clutch from Outplayed's log**; killfeed OCR verifier drops teammate-kill / death frames | $0 | Highest (game-client data) |

The duration split is empirically grounded: probing the library showed
a clean gap (~39 s clips vs ~900 s+ recordings), so the cutoff is
unambiguous.

## RAG + reranker (roadmap, not built)

Today there is **no RAG and no reranker** — and that's correct, because
there's no text corpus to embed yet. The ranker (`ranker.py`) is a
*scoring* step, not a reranker.

RAG becomes meaningful only after the **transcript** source exists
(Whisper STT over the mic/comms track). Then:

```
"find my clutch 1v3 where my teammate raged"
      │
      ▼  embed query
vector search over transcript chunks ──> ~50 roughly-relevant clips
      │
      ▼  reranker (cross-encoder) reorders by true relevance
   top-N semantically-matched clips
```

A **reranker** only appears in *that* pipeline — it reorders retrieved
results. It is unrelated to the current LLM ranker.

## MCP (built, separate package)

The MCP server is the `mcp/` package of this monorepo (next to `api/`,
`web/`, `docs/`; FastMCP, stdio). It
wraps the HTTP endpoints as tools (`scan_assets`, `list_assets`,
`generate_candidates`, `rank_asset`, `analyze_asset`, `get_job`,
`compile_highlights`, `insert_compilation_clip`, …) so the pipeline is
drivable conversationally from Claude. Job-based tools poll internally
(one call = one finished step). It's a thin adapter that only needs the
API URL — registered via `api/.mcp.json`, which points at `../mcp`.
Requires the API (`uv run dev`) to be running. See
[`docs/mcp.md`](docs/mcp.md).

**Backend implication:** never import from `mcp` in `api/`, and never
add MCP-tool code under `api/`. New tools belong in `mcp/`; `api/` only
changes if the underlying HTTP endpoint changes.

### MCP coverage policy — every editing op must be MCP-drivable

**Every editing operation must have a corresponding MCP tool.** The
user drives all editing through Claude / MCP — if an action is only
reachable via raw HTTP, it doesn't exist from their perspective.

When you add a new mutating endpoint, you owe BOTH halves:

1. The HTTP endpoint in `routers/` (mutator + render + journal).
2. A matching MCP tool in `../mcp/src/ai_video_editor_mcp/server.py`
   that calls it. The tool's docstring is what the LLM sees — write
   it like a user manual, not internal docs.

Read-only/inspection endpoints (GET) get tools too when they're part
of an iterative workflow (e.g. `list_compilation_clips`,
`list_compilation_history`).

Naming convention for compilation-editing tools:
- Prefix with the noun: `compilation_…`, `intro_…`, `clip_…`
- Use a verb that matches the user's phrasing: "add the intro after
  clip 3" → `insert_compilation_intro_after`, not `add_intro_v2`
- Keep `clip_ref` parameter naming consistent (1-based index / UUID
  prefix / `"M:SS"` time — same as everywhere else)

## Riot API usage

`candidates/riot.py` uses Riot **ACCOUNT-V1** (resolve `gameName#tag` →
puuid) and **MATCH-V5** (recent match ids → match info + timeline). It
extracts the user's `CHAMPION_KILL` events (kills *and* deaths), then
correlates in two stages: (1) **pick the match** — convert the
Outplayed *filename's* local record-start (DST-aware via
`RECORDING_TIMEZONE`) to UTC and pick the closest match start
(`_correlate`); `st_ctime` is unreliable and unused. (2) **map kills**
— a full recording spans the match, so a kill at game-clock T maps to
offset ≈ T, tunable via `RIOT_SYNC_OFFSET_SECONDS`.

**Correlation is not trusted blindly.** `_confidence` grades each pick
high/medium/low from the start & duration deltas; this is surfaced in
the highlights `index.md` (a LOW pick is loudly flagged as likely the
wrong game), and a non-trusted match is *not* allowed to name the
folder (avoids mislabeling/collisions). Honest-when-unsure by design.

`detect_riot_events` returns `(candidates, status)` where status ∈
`ok|no_match|rate_limited|api_error|disabled`. `compute_candidates`
passes it up as `diagnostics`; the candidates job summary flags a
`rate_limited`/`api_error` (transient, retryable) distinctly from a
genuine `no_match` — a throttled key no longer looks like "no data".

**Per-recording clip offset.** The gap between Outplayed's file start
and Riot's game-clock zero (loading screen) is constant *within* a
recording but differs *between* them — never a global constant.
`calibrate.align_offset` cross-correlates the kill *comb* against the
recording's own per-second loudness (`audio.energy_curve`, decoded
once, shared with `audio_peak`) and returns `(offset, quality)` where
quality is a z-score (≥3 solid). If quality is weak
(`RIOT_OFFSET_MIN_QUALITY`), `ocr.verify_offset` reads the on-screen
clock off a sampled frame as an independent second opinion — fully
graceful (CLI tesseract; returns `ocr_available:false` with a reason if
the binary/HUD/digits don't cooperate, e.g. windowed captures — never
fatal, audio offset stands). Offset, quality and any OCR check are
surfaced in `index.md`.

Constraints: a **dev key expires every 24 h** and only exposes recent
matches — so old recordings won't correlate (expected, not a bug).
Fully config-gated (`RIOT_*`) and non-fatal: any error → `[]`, other
sources still produce candidates.

## Cost model

- Local steps (scan, all candidate generation incl. Riot/audio) cost
  **$0** — no network LLM calls.
- The **only** Anthropic call is `POST /assets/{id}/rank`: exactly one
  `messages.parse` per video, ranking the whole candidate batch.
- Default model `claude-haiku-4-5` (~$0.005/video). Hard guard
  `ANTHROPIC_MAX_RANK_CALLS` (default 25/process) refuses further calls
  with a clear error and **no API call** when capped.
- Prompt caching: the ranker's system prompt is a frozen, `cache_control`
  block; volatile candidate JSON is in the user turn.

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
finished round. Run heavy steps **one at a time** (the laptop has ~1 GB
free with the owner's apps open) and log a memory audit under
`WORKSPACE/audits/memory/<date>_<game>/`.

1. VOD local + asset row (pull from Supabase if it came from the PC).
2. `POST /assets/{id}/transcribe` (full audio) and `/transcribe-mic`.
3. `POST /assets/{id}/candidates` (also caches the Riot match/timeline) →
   `/rank` → `/highlights` (cuts clips into `highlights/<game>/<session>`).
4. `league-understand --asset <id>` → `WORKSPACE/plays/<id>.json`.
5. Panel round `WORKSPACE/panel/<YYYY-MM-DD_HHMM_champ_K-D-A>/`: brief →
   gaming/viewer/editing experts in parallel → mentor chairs →
   `clips.json` + `panel-decision.md`.
6. `story-render --asset <id> --round <round> --plays`.
6a. Optional, before narrating: `story_suggest_edits` → owner ticks → `story_apply_suggestions` (Jev, no Anthropic; `docs/story-suggestions.md`).
6b. Narrate in the web page (`/narrate/story/<round>`); export → `narrated.mp4`.
7. Owner watches the story; fixes are recorded with `mentor_record_correction`.
8. Drive: `grokbot-publish-round <round> --game league --asset-id <id>
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
