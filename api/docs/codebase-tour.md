# Codebase Tour (plain language)

What every file does, the real data flow, and **which parts are solid
vs. known-shaky** — so you can fine-tune with eyes open. No jargon.

## The one-sentence model

Find cheap "maybe interesting" moments with zero AI → ask one small LLM
call to score/keep them → cut the kept ones into tidy folders. Source
videos are never modified; everything is read-only + outputs to a
separate workspace.

## Walkthrough (follow a request through it)

```
scan ─▶ assets table ─▶ candidates ─▶ rank (1 LLM call) ─▶ highlights folder
```

| File | Plain English | Trust |
|---|---|---|
| `main.py` | Boots the FastAPI app, wires routers. | solid |
| `config.py` | Every setting/knob (`.env`). One place. | solid |
| `database.py` | SQLite tables + raw SQL. Postgres-portable. | solid |
| `models.py` | Request/response shapes; `CandidateSource` list. | solid |
| `routers/assets.py` | `scan` walks the Outplayed folder → DB rows. | solid |
| `routers/clips.py` | Trim one clip (background job). | solid |
| `routers/projects.py` | Timeline + render a rough cut. | solid (Phase 1) |
| `routers/analyze.py` | The Phase-2 HTTP layer: candidates / rank / rankings / highlights / batch jobs. Polled via `/jobs/{id}`. | solid |
| `editing.py` | The single shared ffmpeg `trim_clip`. | solid |
| `candidates/probe.py` | ffprobe duration. | solid |
| `candidates/service.py` | **The orchestrator.** Decides which sources run for a video and assembles candidate rows. Start here to understand flow. | solid |
| `candidates/audio.py` | Decodes audio once → per-second loudness → loud-region candidates. | solid |
| `candidates/riot.py` | Riot ACCOUNT/MATCH-V5: who you are → match history → pick the match by filename time → your kills. Confidence-graded. | **data solid; match-pick is the historically fragile part** |
| `candidates/calibrate.py` | Tries to auto-find the recording↔game offset by aligning kills to loudness. | **shaky — can be confidently wrong; fallback only** |
| `candidates/ocr.py` | Reads the on-screen clock off a frame (tesseract) as a backup offset check. | **best-effort; fails on windowed captures** |
| `ranker.py` | The only paid step. One `messages.parse` scores the whole candidate batch (keep + funny/hype/story). Spend-capped. | solid |
| `highlights.py` | Cuts kept suggestions into `highlights/<game>/<date>_<champ|time>/` + `index.md/json`. Per-source clip-window strategies. | solid |
| `tracing.py` | Langfuse init. (Token usage not captured — see roadmap.) | partial |
| `mcp_server.py` | Thin MCP adapter over the HTTP API (no logic of its own). | solid |

## The offset problem (the thing that ate this week)

Riot tells us *when* a kill happened on the **game clock**. Your video
file starts at some unknown point (loading screen). The gap between them
is the "offset". Getting it wrong = clips full of walking, no kills.

- `calibrate.align_offset` tries to guess it from audio — **it lies
  confidently**, don't trust it.
- `ocr.verify_offset` tries to read the on-screen clock — **dies on
  windowed/overlay captures** (yours).
- **What actually works:** `RIOT_OFFSET_OVERRIDE_SECONDS` — one number
  you measure once from a wide calibration clip (`true offset =
  VOD_position_of_a_kill − its_Riot_game_clock`). `service.py` uses it
  verbatim and skips the guessers. `index.md` shows
  `offset_source: manual_override` so you know it's the trustworthy run.
- The real long-term fix is **CV detection** (see roadmap): find kills
  from the pixels, and the offset question disappears.

## Where to poke when fine-tuning

- Clip length / context: `HIGHLIGHT_PRE_SECONDS` / `_POST_SECONDS`.
- Which moments survive: `ranker.py` system prompt + `keep` logic.
- Offset for a recording: `RIOT_OFFSET_OVERRIDE_SECONDS` in `.env`.
- New candidate source: add `candidates/<x>.py` → register in
  `service.compute_candidates` → add to `CandidateSource`. Nothing else
  changes (open/closed).
- New clip-window behavior: add a `_window_*` in `highlights.py` +
  registry entry.

## Honest state

Phases 1–2 + highlights + MCP are solid and do real work. The Riot
*kill data* is accurate. The Riot *correlation/offset* is the fragile
corner — usable now via the manual override, properly fixed later by CV.
Everything else (scan, audio, ranking, highlights, batch) is dependable.
