# Candidate Sources

Every source emits rows with the same shape (`start_seconds`,
`end_seconds`, `confidence`, `event_type`, `metadata`) so the ranker
treats them uniformly. `candidates/service.py` composes them per asset.

## `outplayed_clip`

If a file's duration ≤ `OUTPLAYED_CLIP_MAX_SECONDS` (default 120 s), it
*is* one of Outplayed's auto-cut event clips — Outplayed already did the
hard detection. The whole file becomes one high-confidence (0.9)
candidate. Cost: a single `ffprobe`. The empirically clean
clip/recording duration gap makes this classification unambiguous.

Because Outplayed already curated these, the **batch** flow
(`POST /clips/batch-highlights`) deliberately *skips the LLM* — an
`outplayed_clip` candidate carries no signal to rank on, so a rank call
would be a paid no-op. It just organizes the clips, newest-first, into
`highlights/<game>/clips_<date>/`. This is the right tool for Valorant
(no Riot data) and for clearing large clip backlogs at $0.

## `riot_api` (League of Legends)

The highest-quality source: **ground-truth** kill data.

1. `ACCOUNT-V1`: resolve `RIOT_ID` (`gameName#tagLine`) → puuid.
2. `MATCH-V5`: recent match ids → each match's info + timeline.
3. Correlate: the recording's wall-clock window (`created_at` +
   duration) is matched to the Riot match whose game window overlaps
   most (`_pick_match`).
4. Extract the user's `CHAMPION_KILL` events (kills *and* deaths);
   each game-clock timestamp maps to an offset in the recording, padded
   by `ANALYZE_WINDOW_PADDING`. Confidence 0.95.

Config-gated by `RIOT_API_KEY` / `RIOT_ID` / `RIOT_REGION`. Non-fatal:
any failure → `[]`. **Dev keys expire every 24 h** and only expose
recent matches, so recordings older than the lookback window won't
correlate — expected, not a bug. Correlation helpers are pure and
unit-tested without a key.

## `audio_peak` (full recordings)

For recordings longer than the clip cutoff: extract a low-rate
(8 kHz) mono WAV with ffmpeg, compute per-second RMS energy with numpy,
normalize, threshold (`ANALYZE_PEAK_THRESHOLD`), cluster adjacent loud
windows, pad, and keep the strongest up to `ANALYZE_MAX_CANDIDATES`.
"Loud ≈ action" — a weak but free, game-agnostic signal.

Guarded: `ANALYZE_AUDIO_MAX_SECONDS` skips over-long recordings, and a
disk preflight refuses extraction unless free space ≥ estimated WAV
size + `MIN_FREE_DISK_MB`. The temp WAV is always cleaned up.

## `transcript_keyword` (built)

Local **faster-whisper** STT (`transcribe.py`, CPU by default —
private, $0, no upload) runs as its own explicit job
(`POST /assets/{id}/transcribe`) and stores timestamped segments in the
`transcripts` table. `transcript.py` is then a *pure* function over
those segments: it scans for spoken hype/funny cues ("let's go",
"insane", "clutch", "lmao", …) and emits `hype_callout`/`funny_callout`
candidates for the ranker. Game-agnostic (works for Valorant too).
The stored transcript is a reusable corpus — also the foundation for
RAG/semantic search (see roadmap).

## `overwolf_game_event` (reserved)

Overwolf's Game Events API only works live, inside an Overwolf capture
app — it cannot be queried for past recordings. Reserved for a future
capture-time integration.

## Adding a source (open/closed)

1. New `candidates/<name>.py` with `detect_<name>(...) -> list[dict]`.
2. Register it in `service.compute_candidates`.
3. Add the string to `CandidateSource` in `models.py`.

The ranker and routers do not change.
