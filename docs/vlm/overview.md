# VLM Taste Layer — Overview

> **TL;DR** — A local Qwen3-VL 8B/4B/2B (via Ollama) sits on top of the
> existing candidate-first pipeline as a *taste layer*. Cheap finders
> keep finding moments; the VLM validates each cut clip before it lands
> and reviews the whole compilation for pacing. Every call is a
> Langfuse span so the whole workflow is inspectable as a tree.

---

## Why this exists

Today's pipeline produces highlight clips through cheap deterministic
finders (Riot API, audio peaks, Whisper transcripts) plus one Anthropic
LLM ranker call per video. It works, but three problems keep surfacing:

- **Wrong windows** — right moment, wrong cut (starts too late, ends
  mid-action)
- **False positives** — `audio_peak` fires on shouting that wasn't a
  kill; the finder said "interesting" and the clip is boring
- **Val gap** — no Riot API for Valorant, so recall + precision are
  weaker there

The VLM validates every clip against its **actual visual content**
without paying the crazy per-frame cost of asking a hosted vision
model. It runs locally, so cost per compilation is **$0** regardless
of how many iterations it takes.

---

## Architecture at a glance

```
recordings ──> finders (riot_api / audio_peak / transcripts / outplayed_clip)
                                      │
                                      ▼
                        HighlightCandidate rows in SQLite
                                      │
                                      ▼
                     ranker (1 Anthropic call, structured JSON)
                                      │
                                      ▼
                       kept ranked suggestions
                                      │
              ┌───────────────────────┴─────────────────────────┐
              │                                                 │
              ▼                                                 ▼
    ┌───────────────────┐                            ┌──────────────────────┐
    │ per-clip loop     │                            │ initial compile      │
    │  ─ cut window     │                            │  ─ per-clip render   │
    │  ─ sample frames  │                            │  ─ concat            │
    │  ─ ask VLM        │                            │  ─ optional music    │
    │  ─ pass / skip /  │                            └──────────┬───────────┘
    │    retry N times  │                                       │
    └────────┬──────────┘                                       ▼
             │                                    ┌──────────────────────────┐
             ▼                                    │ whole-comp review loop   │
     validated clip files                         │  ─ sample 30-60 frames   │
             │                                    │  ─ ask VLM for pacing    │
             ▼                                    │    / cohesion / variety  │
   compilation.mp4 rendered                       │    suggestions           │
                                                  │  ─ return fixes list     │
                                                  └──────────────────────────┘
```

---

## The two loops

### Per-clip loop (max 5 iterations)

For every ranker-kept candidate, before the final cut lands:

1. Cut a candidate sub-clip from the source (`highlights.py::trim_clip`)
2. Extract 8 frames via `vlm/frames.py::extract_frames` (`ffmpeg -ss ... -frames:v 1`)
3. Ask VLM (system prompt = base template + `game_hints/<game>.md`):

   > *"Verdict on this clip? claimed event: kill · source: riot_api ·
   > anchor: t=754.2s · clip length: 12.5s · watch the sampled frames
   > and return the JSON verdict."*

4. Parse the JSON verdict:

   ```json
   {
     "verdict": "pass" | "fixable" | "false_positive",
     "why":     "kill happens at 0:03 but clip cuts in at 0:02, no setup",
     "fix":     "extend_before" | "extend_after" | "trim_start" | "trim_end" | null,
     "fix_seconds": 2.0
   }
   ```

5. Route on `verdict`:
   - **pass** → keep, exit loop, write final path
   - **false_positive** → delete cut file, skip clip, no retries wasted
   - **fixable** → shift window per `fix` + `fix_seconds`, re-cut, retry

After 5 fixables: keep the last cut but mark it "cap reached" so the
user sees it in the review.

### Whole-comp review loop (max 3 passes, review-only in v1)

After per-clip loops finish and the compilation is rendered:

1. Sample 40 frames spread across the rendered `compilation.mp4`
2. Ask VLM for a list of pacing / cohesion / variety issues:

   ```json
   {
     "is_cohesive": false,
     "fixes": [
       {"clip_ref": "03", "issue": "same event_type as clip 02", "fix": "remove_clip"},
       {"clip_ref": "07", "issue": "much longer than others", "fix": "trim_end", "fix_seconds": 3.0},
       {"clip_ref": "05", "issue": "kill visually weak", "fix": "apply_zoom", "roi": "champion_portrait_lol"}
     ]
   }
   ```

3. Return the suggestions.

In v1 the loop is **review-only** — it returns suggestions but doesn't
mutate the spec. You apply the ones you want via the existing MCP
editing tools (`extend_compilation_clip`, `zoom_compilation_clip`,
`remove_compilation_clip`, etc.). Auto-apply is a queued follow-up.

---

## Data flow, end to end

1. **Scan** — `POST /assets/scan` finds new recordings in the Outplayed
   folder, writes rows to the `assets` table with `game` inferred from
   the parent folder.
2. **Rank + compile** — `POST /assets/{id}/compile` triggers the
   pipeline. Behind the scenes:
   1. `candidates/service.py` runs all finders, writes
      `highlight_candidates` rows.
   2. `ranker.py` sends the candidate batch to Claude (**the only**
      paid step in the entire flow, ~$0.005 per video).
   3. `highlights.py::build_highlights` iterates kept rankings. **This
      is where the per-clip VLM loop hooks in.** For each candidate:
      - `vlm.loops.validate_and_cut` runs the 5-iteration loop against
        Ollama; only clips that `pass` or hit the retry cap end up on
        disk. `false_positive` verdicts leave zero output.
   4. `compile.py::build_compilation` renders the compilation from the
      surviving clips (per-clip render → concat → optional music mix).
3. **Whole-comp review** — either automatically at the end of compile
   (if `?vlm_review=true`) or manually via
   `POST /edit/compile/{id}/vlm_review`. Returns the fixes list; UI
   shows them in the sidebar; user picks which to apply.

---

## The verdict schemas (Pydantic-validated)

Both live in `api/src/ai_video_editor/vlm/prompts.py`.

### `ClipVerdict` — per-clip

| Field | Type | Notes |
|---|---|---|
| `verdict` | `"pass"` \| `"fixable"` \| `"false_positive"` | The routing signal |
| `why` | `str` | One-sentence explanation; logged + shown in UI |
| `fix` | `"extend_before"` \| `"extend_after"` \| `"trim_start"` \| `"trim_end"` \| `null` | Only set on `fixable` |
| `fix_seconds` | `float` (0.1-8.0) | Bounded — a hallucinated 999s extension gets rejected at parse time |

### `CompilationReview` — whole-comp

| Field | Type | Notes |
|---|---|---|
| `is_cohesive` | `bool` | Loop exits early when `true` |
| `fixes` | `list[CompilationFix]` | Suggested edits |

Each `CompilationFix` carries `clip_ref`, `issue`, `fix`, plus
type-specific fields (`fix_seconds` for extend/trim, `roi` for zoom,
`focus_x`/`focus_y` for focus).

---

## Multi-game extensibility

Per-game prompt content lives in
`api/src/ai_video_editor/vlm/game_hints/`:

- `league.md` — HUD layout, champion vocabulary, common false
  positives, event lexicon (first blood, penta kill, baron steal…)
- `valorant.md` — HUD layout, agent vocabulary, kill-feed position,
  event lexicon (ace, clutch, 1v3…)
- `_default.md` — Fallback for unknown games

**Adding a new game = new file, zero code.** The loop mechanics,
verdict schema, and backend are game-agnostic. The `game` field on
the asset row (already inferred by `scan_assets`) picks the file.
Unknown games fall back to `_default.md`.

---

## Backend selection

The `VLMBackend` protocol (`vlm/backends/base.py`) is the seam.
Today the only implementation is `OllamaBackend`; the protocol makes
adding a hosted backend a one-file PR.

### The Ollama backend

- Talks HTTP to `localhost:11434` using Ollama's `/api/chat` with
  `format: "json"` (structured output).
- Frames go as base64 on the user message per Ollama's schema.
- **Model fallback ladder**: tries `VLM_MODEL_PRIMARY` first, then
  `VLM_MODEL_FALLBACK`. If neither is pulled OR Ollama is unreachable,
  the loop degrades to `pass` verdicts and the compile proceeds
  without validation. **VLM is a filter, not a gate** — a broken
  filter must not drop clips.
- **Current defaults for this repo** (tuned for 6 GB VRAM /
  GTX 1660 class):
  - Primary: `qwen3-vl:4b` (~3.3 GB quantized)
  - Fallback: `qwen3-vl:2b` (~2 GB quantized)

### Cost + speed reality

| GPU class | Model | Cold-start | Warm call | 30 calls added |
|---|---|---|---|---|
| RTX 4090 24GB | `qwen3-vl:8b` | ~5s | ~2-4s | +1-2 min |
| RTX 4070 12GB | `qwen3-vl:8b` | ~15s | ~5-10s | +3-5 min |
| **GTX 1660 6GB (this box)** | `qwen3-vl:4b` | ~17s | ~5-10s | +3-8 min |
| Any 8GB | `qwen3-vl:4b` 4bit | ~20s | ~10-30s | +5-15 min |
| CPU only | `qwen3-vl:2b` | ~30s | ~30s-2min | +15-40 min |

Cost per compilation: **$0**, always. That's the point.

---

## Langfuse traceability

Every VLM call is `@observe`-decorated using the exact pattern from
`ranker.py:25-36,112,163-183` — no OpenTelemetry, no manual span
creation. Optional import shim keeps the module runnable without
Langfuse configured (no-op `@observe` fallback).

### What the trace tree looks like

Real trace of a 4-clip compilation:

```
compile_asset:my_scrim_2026_05_26                     [root]
├─ rank_asset                                         (existing Anthropic ranker span)
├─ initial_compile
└─ vlm_review                                         [tag: vlm, backend: ollama, model: qwen3-vl:4b]
   ├─ vlm-validate-clip clip:01 (event: kill@12:34)
   │  ├─ vlm-per-clip-iter 1  → fixable(fix=extend_before 2.0s, why="no setup visible")
   │  ├─ vlm-per-clip-iter 2  → fixable(fix=trim_end 1.5s, why="dead air after payoff")
   │  └─ vlm-per-clip-iter 3  → pass ✓
   ├─ vlm-validate-clip clip:02 (event: audio_peak@18:02)
   │  └─ vlm-per-clip-iter 1  → false_positive (why="no fight, teammate laughing")
   │                            [SKIP: no retries wasted]
   ├─ vlm-validate-clip clip:03 (event: kill@22:47)
   │  └─ vlm-per-clip-iter 1  → pass ✓
   ├─ vlm-validate-clip clip:04 (event: kill@31:15)
   │  └─ vlm-per-clip-iter 1  → pass ✓
   ├─ vlm-whole-comp-pass 1
   │  ├─ fix: apply_zoom clip:01 roi=champion_portrait_lol
   │  └─ fix: trim_end clip:03 seconds=2.0
   ├─ vlm-whole-comp-pass 2
   │  └─ fix: apply_focus clip:04 x=0.5 y=0.5
   ├─ vlm-whole-comp-pass 3
   │  └─ pass ✓ (compilation is cohesive)
   └─ [DONE]
```

### What each span carries

- **model** — which fallback tier is active (`qwen3-vl:4b` vs `2b`)
- **backend** — `ollama` today
- **metadata.hints_file** — which `game_hints/*.md` was resolved
  (`league`, `valorant`, `_default`)
- **metadata.frames** — sampled frame count
- **input** — the exact user prompt
- **output** — the parsed verdict (verdict, fix, fix_seconds) or
  review (is_cohesive, num_fixes)
- **tags** — `["vlm", "ollama", "per-clip"]` or `[..., "whole-comp"]`

The nested structure means one click at any level surfaces exactly
what was fed in and what came out.

### Notes vs the ranker

- The ranker uses `@observe(as_type="generation")` because Anthropic's
  `messages.parse` returns billed tokens. The VLM's Ollama backend
  doesn't return tokens (they're local + free), so we omit
  `usage_details` — Langfuse just shows the call, no billing math.
- `AnthropicInstrumentor` covers the ranker's `messages.create` path
  automatically. It does **not** touch our VLM path (no Anthropic
  calls) — every VLM span is our own hand-instrumented one.

---

## Configuration knobs

Everything under `VLM_*` in `.env` (defaults in `config.py`):

```bash
# Master switch — flip false to run the pipeline without any VLM
VLM_ENABLED=true

# Backend selection (only "ollama" today)
VLM_BACKEND=ollama

# Loop bounds
VLM_MAX_CLIP_ITER=5        # per-clip iterations before skip
VLM_MAX_COMP_ITER=3        # whole-comp review passes

# Frame budgets (higher = more accurate, slower)
VLM_FRAME_SAMPLES_CLIP=8
VLM_FRAME_SAMPLES_COMP=40

# Ollama backend
VLM_OLLAMA_URL=http://localhost:11434
VLM_MODEL_PRIMARY=qwen3-vl:4b
VLM_MODEL_FALLBACK=qwen3-vl:2b
VLM_CALL_TIMEOUT_SECONDS=120
```

## Key files (for anyone reading source)

- **Loop control** — `api/src/ai_video_editor/vlm/loops.py`
- **VLM calls** — `api/src/ai_video_editor/vlm/validator.py`
- **Backend** — `api/src/ai_video_editor/vlm/backends/ollama_backend.py`
- **Prompts + schemas** — `api/src/ai_video_editor/vlm/prompts.py`
- **Game hints** — `api/src/ai_video_editor/vlm/game_hints/*.md`
- **Frame sampler** — `api/src/ai_video_editor/vlm/frames.py`
- **HTTP routes** — `api/src/ai_video_editor/routers/vlm.py`
- **MCP tools** — `mcp/src/ai_video_editor_mcp/server.py`
  (`vlm_health`, `vlm_review_compilation`)
- **Frontend** — `web/src/components/VLMReviewPanel.tsx`,
  `web/src/hooks/useCompilation.ts` (`vlmHealth`, `vlmReview` entries)

---

## Out of scope for this version (queued follow-ups)

- **Auto-apply mode** for whole-comp fixes — today it's review-only.
  Auto-apply requires wiring each fix type to the existing mutator
  endpoints; queued for a small follow-up.
- **`revalidate_compilation_clip`** MCP tool — needs re-cutting from
  source, more surface than a spec mutation.
- **Val HUD killcount finder** — only worth building if the VLM
  validator alone doesn't recover Val recall in practice.
- **Hosted VLM backend** (Anthropic vision / Gemini / OpenRouter /
  HuggingFace free tier) — the `VLMBackend` protocol is ready; one
  file to add.
- **Time-scoped effects** — zoom on 2 seconds inside a longer clip,
  independent of the compile mode work.
