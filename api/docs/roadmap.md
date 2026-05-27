# Roadmap & Backlog (living doc)

This is the durable list of where things stand, what's next, and the
**open questions to discuss before implementing**. Update it as we go;
don't let ideas live only in chat.

## Where we are (built & working)

- **Phase 1** — scan → clip (ffmpeg) → projects/timeline → render.
- **Phase 2** — candidate-first analyzer: `outplayed_clip`, `audio_peak`,
  `riot_api`; LLM ranker (Haiku, spend-guarded); Langfuse.
- **Highlights** — cut kept suggestions into organized
  `highlights/<game>/<date>_<champ|time>/` folders + `index.md`.
- **Batch** — pool short Outplayed clips, no LLM ($0).
- **MCP server** — drive it all conversationally (see [mcp.md](mcp.md)).
- **Riot correlation** — filename-time match pick + confidence grading +
  rate-limit/no-match distinction + **manual per-recording offset
  override** (the reliable path).
- **Transcripts (Whisper STT)** ✅ — local faster-whisper CPU, $0.
- **RAG semantic search** ✅ — `fastembed` + `sqlite-vec`; `search_clips`
  MCP tool.
- **Phase 3 editing** ✅ — per-clip primitives, `/edit/compile`,
  iterative compilation editing (`spec.json` source of truth),
  `insert_clip`.
- **Langfuse token/cost** ✅ — manual `usage_details` since
  `messages.parse` isn't covered by `AnthropicInstrumentor`.

## Lessons learned (don't repeat these)

- **Audio↔kill cross-correlation is unreliable.** It produced a
  *confidently wrong* offset (+32s when truth was −17s). High lock
  "quality" ≠ correct. It's demoted to a fallback; the trustworthy path
  is `RIOT_OFFSET_OVERRIDE_SECONDS` from one human ground-truth point.
- **Riot kill *data* is accurate**; the only hard problem was ever the
  recording↔game-clock **offset**. Don't keep auto-tuning offset — it's
  diminishing returns. CV detection (sprint #5) sidesteps it entirely.
- **`st_ctime` is not record time**; the Outplayed *filename* timestamp
  is the usable signal (still ambiguous start vs finalize).
- **Dev key**: 24 h expiry, limited match history. Production key or a
  cached match store needed to backfill old recordings.
- **Cost**: ranking is the only paid step (~$0.026 / 34-candidate rank
  on Haiku); whole project to date ≈ $0.25–0.30. Everything else is
  $0/local.

## Active sprint (next, in order)

Locked sequence — we discuss the **Open questions** on each item before
writing code. Each unlocks the next.

### 1. Clustering — merge ranked candidates within ~30s window ✅

`cluster_ranked_candidates(rankings, candidates, gap_seconds)` in
`compile.py` does post-rank merging — overlapping/within-gap kept
rankings collapse into one fused clip. Anchor selection prefers the
highest-confidence `riot_api` candidate in the cluster; falls back to
highest-hype if no riot source is present. Aggregated scores are
`max()` across the cluster; merged reason is tagged `[merged Nx]`.
Wired into `build_compilation` between "load rankings" and
`spec_from_rankings`; tunable via `settings.cluster_gap_seconds`
(default 30s, `0` disables). 13 unit tests cover the algorithm.

### 2. Game profile system — regions + sound names + SFX extraction ✅

`src/ai_video_editor/profiles/` ships TOML registries (`league.toml`,
`valorant.toml`, `default.toml`) loaded lazily and cached per-process
via `load_profile(asset.game)`. Schema is `[meta] / [regions.<name>]
/ [sounds.<name>]`; coords are fractions in `[0, 1]` and the validator
rejects boxes that extend past the frame edge. Lookup is
case-insensitive across `meta.game` + `meta.aliases` ("League of
Legends" → "league" profile). Unknown game → `default` profile (empty
vocab, primitives still work, game-specific tools warn-and-skip).

`POST /sfx/extract` cuts an audio span out of an asset into
`WORKSPACE/media_library/<canonical-game>/sfx/<file>.wav` (mono
44.1 kHz PCM — the shape sprint #4's mel matcher expects). Aliases
all route to the same canonical directory; undeclared sound names
are still saved with a warning so the user knows to add an entry to
the profile.

v1 LoL regions: `minimap`, `scoreboard`, `tab_overlay`, `killfeed`,
`champion_portrait`, `item_bar`, `hp_mana`. LoL sounds: `first_blood`,
`enemy_slain`, `ace`, `penta_kill`, `baron_slain`, `buy_item`.

v1 Valorant regions: `minimap`, `scoreboard`, `tab_overlay`,
`killfeed`, `crosshair`, `agent_card`, `ult_orbs`, `money_display`.
Valorant sounds: `cash_pickup`, `headshot_ping`, `kill_confirmed`,
`ace`, `spike_planted`, `spike_defused`.

Region coordinates are starting guesses for 1920×1080 default-HUD-scale;
**calibrate against a real recording** before sprint #3's
`zoom_region` consumes them.

### 3. `zoom_region` + `add_sfx` + `add_card` primitives + per-game MCP wrappers

The immediate ask — per-game MCP toolsets. One generic primitive each
in the backend, plus auto-generated game-prefixed MCP tools driven off
the profile.

Generic primitives:
- `zoom_region(clip_ref, region_name)` — resolves through `asset.game`'s
  profile to a fractional box, then re-renders that clip with the
  existing zoom effect.
- `add_sfx(clip_ref, anchor_seconds, sound_name, duck=true)` — overlays
  the named sound from the profile at the anchor, ducking source audio.
- `add_card(position, text, duration, sfx?)` — black-bg interstitial
  between clips. Replaces the old "Milestone D+" entry.

Auto-generated per-game wrappers (derived from each profile):

```
league_zoom_minimap(clip_ref)
league_zoom_scoreboard(clip_ref)
league_play_first_blood(clip_ref, at)
league_play_ace(clip_ref, at)
league_play_pentakill(clip_ref, at)
valorant_zoom_minimap(clip_ref)
valorant_play_headshot(clip_ref, at)
valorant_play_buy_cash(clip_ref, at)
valorant_play_ace(clip_ref, at)
…
```

Game-prefixed names give the LLM a discoverable vocabulary; underneath
they call the generic primitives with prefilled args.

**Open questions:**
- Tool naming: `league_*` (full word — lean: clarity > brevity for LLM
  tool-picking) vs `lol_*`.
- Card behavior: **interstitial** (replaces the frame, sits between
  clips — lean) vs **overlay** (drawn on top of a clip). v1: interstitial.
- `add_sfx` ducking: sidechain duck (-6 dB during sfx) by default, opt
  out per call. Configurable threshold/release in v2 if needed.
- Where to source the per-game tool list: backend exposes
  `GET /profiles/<game>/tools`; the MCP repo reads it at startup and
  decorates wrappers. Profile changes flow without backend redeploy
  (MCP restart only).
- Graceful failure: a tool that calls a region/sound the profile lacks
  → warn, skip, return a clear error string (never crash the reel).

### 4. Audio event detection — new `audio_event` candidate source

Announcer audio (First Blood, Ace, headshot ping, round-end stings) is
**already in the source audio** — the game played it. Cross-correlate
the recording's mel-spectrogram against the profile's reference
templates → exact timestamps for every announcer event. Each detection
emits **both**:

- a `HighlightCandidate` row (signal for the ranker), **and**
- a structured event tag (signal for the editor — auto-place the sfx
  stinger at that anchor with `add_sfx`).

One detector, two consumers. Sidesteps the lack of a Valorant kill
timeline. Free, deterministic, no model.

**Open questions:**
- Matcher: pure cross-correlation on log-mel features (lean — no ML
  deps, $0) vs a tiny pretrained fingerprint model. Mel cross-corr is
  enough for ~3 s announcer stingers.
- Detection threshold per template (configured in the profile) — some
  templates are more distinctive than others.
- Hop length: 100 ms is fine for announcer cues; tune up to 200 ms if
  cost becomes an issue.
- Missing `media_library/<game>/sfx/` → source returns `[]` silently
  (matches the "fail safe, never fatal" convention from `riot.py`).
- Reuse the existing audio decode? Yes — `audio_peak` already extracts
  a low-rate mono WAV. Share the buffer.
- Output candidate shape: `event_type: "announcer_event"`,
  `metadata.sound_name: "first_blood"`, so editor + ranker both
  introspect the same field.

### 5. CV killfeed detection — new `killfeed` candidate source

Frame-based kill detection. Kills the offset class entirely *and* gives
**Valorant** kill events at all (no public Riot-V5-equivalent timeline
for Val). The killfeed sits in a fixed UI region in both games.
Template-match against Data Dragon champion portraits (LoL) / agent
icons (Val). Sample at 2–4 fps inside the profile's `killfeed` region.

Output candidates carry `event_type=kill`, attacker/victim metadata,
and play with existing `_window_*` strategies (anchor at killfeed-row
appearance ± `HIGHLIGHT_PRE/POST`).

**Open questions:**
- Matcher: template matching against Data Dragon portraits (lean —
  exact, free, no training) vs a small CNN. CNN only if templates fail
  on resolution/HUD-scale variants.
- Frame sampling: 2 fps (cheap, may miss brief multi-kill rows) vs
  4 fps (safer, 2× cost). Lean: 4 fps, measure.
- Coexistence with `riot_api`: **yes** — Riot still owns ground-truth
  game-clock data (used for folder naming, stats); killfeed CV is its
  own observation source. `service.compute_candidates` merges/dedupes.
- Where it runs: a long-running job (CV pass is multi-minute on a long
  VOD), like `transcribe`. Don't block `compute_candidates`.
- Data Dragon mirroring: fetch portraits once at first use, cache to
  `WORKSPACE/_cache/datadragon/<version>/`.
- Valorant: agent icons aren't on Data Dragon — ship a vetted set in
  the repo or fetch from the Riot Val content endpoint at first use.

### 6. LLM compilation planner — pre-compile spec generation

Today the compile pipeline is mechanical (caption + fade + concat).
With sprints 1–5 done we have rich context to feed an LLM that **plans
the spec** instead of you placing every effect manually.

**Inputs:** ranked candidates + transcript chunks near each anchor +
detected `audio_event`s + `killfeed` events + Riot timeline + the game
profile (so the LLM only emits *available* regions/sounds).
**Output:** a complete `spec.json` with clips ordered, captions written,
effects placed (`zoom_region`, `add_sfx`, `add_card`). Existing renderer
+ iterative-edit tools work unchanged on the planner-produced spec.

**Open questions:**
- One Anthropic call per compile (~$0.02 on Haiku, ~$0.05 on Sonnet) —
  gated by the existing `ANTHROPIC_MAX_RANK_CALLS` counter (or its
  own cap).
- Output mode: `messages.parse` with the spec schema as the response
  type (constrains hallucination; planner can't invent region names).
- Context budget: ±5 s of transcript per candidate; cap total tokens
  to bound cost.
- Roll-out: behind `compile_highlights(plan=True)` flag, off by default
  for v1; A/B against mechanical default before flipping.
- Failure mode: planner output invalid / spend-capped → fall back to
  today's mechanical compile (no broken render).
- Model: probably want Sonnet for the planner (more nuanced editing
  decisions) even if Haiku is fine for ranking. Per-step model
  override (`PLANNER_MODEL`).
- Manual iteration still works after: yes — planner output is just a
  spec, user mutates it via existing iterative-edit MCP tools.

## Done — pre-sprint phases

(Closed; kept for reference.)

### Phase 3, Milestone A — per-clip primitives ✅
`edits.py` + `routers/edits.py`: `zoom`, `caption` (auto-pull from
transcript), `focus` (spotlight via `geq`). Configurable aspect
(16:9 / 9:16).

### Phase 3, Milestone B — `/edit/compile` ✅
Per-clip ffmpeg (caption + 0.3 s fade-in/out + aspect tail) → concat
demuxer → optional music mix. MCP tool `compile_highlights`.

### Phase 3, Milestone B+ — Iterative compilation editing ✅
`spec.json` is the source of truth; mutations re-render only the
affected clip and re-concat. `clip_ref` accepts index, UUID prefix, or
`"M:SS"` (reel-time first, then source).

### Phase 3, Milestone B+1 — `insert_clip` ✅
Add a manual clip from any indexed recording's source range.
Chronological-within-asset by default. Tests in `tests/test_compile.py`.

### Transcripts (Whisper STT) ✅
Local faster-whisper CPU int8, private, $0. `transcribe` job →
`transcripts` table → `transcript_keyword` candidate source.

### RAG semantic search ✅
Local `fastembed` (BAAI/bge-small-en-v1.5) + `sqlite-vec`; `index` job
chunks transcripts into ~25 s windows; `GET /search` + MCP
`search_clips`.

### Langfuse token/cost ✅
Ranker is `@observe(as_type="generation")` and attaches model + token
usage manually (since `AnthropicInstrumentor` doesn't cover
`messages.parse`). Pre-fix calls show $0 — Anthropic Console is
authoritative for those.

## Later / lower priority

- **Auto-assembly (Phase 3)** — kept suggestions → timeline → rendered
  montage. Effectively *becomes* the planner's output once sprint #6
  lands.
- **Postgres** — when concurrency/multi-user arrives. Schema already
  portable (driver + DDL swap).
- **Overwolf capture app** — live game events at record time. Large,
  separate track.
- **Meme/SFX/music library (general user-curated)** — distinct from the
  per-game stingers managed under `media_library/<game>/sfx/` in
  sprints 2–4. `media_library/{memes,sfx,music}/` for user-picked
  reactions/music/generic sfx. Defer until sprint 1–6 prove their value.
- **Cross-encoder reranker** — only if vector search precision suffers
  after real usage. Drop-in on top of the top-50 vector hits.
- **GraphRAG** — explicitly not pursuing. The corpus per video is small
  and queries are flat; vanilla embedding RAG + richer LLM context
  earns the same win without graph infra.

## How to use this doc

Each Active-sprint item has **Open questions** on purpose — we discuss
and decide those *before* writing code (the user wants design
discussion first, not surprise implementations). Move items between
sections as state changes; keep the "Lessons learned" list honest.
