# VLM Testing Guide — End-to-End

> A step-by-step walk of running a real compile with the VLM taste
> layer on and inspecting the resulting Langfuse trace tree. Assumes
> Ollama + `qwen3-vl:4b` are already installed on your machine.

---

## Prerequisites (check once)

**Ollama running + model pulled.** From any terminal:

```bash
# Should list qwen3-vl:4b in the output
curl -s http://localhost:11434/api/tags | grep qwen3-vl

# If nothing prints, pull it:
ollama pull qwen3-vl:4b
```

**API server up with the VLM routes.** The API doesn't auto-reload —
after every code deploy, restart `uv run dev` in the API terminal.
Verify the routes are registered:

```bash
curl -s http://localhost:8000/openapi.json | grep vlm
```

Should show `/api/v1/vlm/health` and `/api/v1/edit/compile/{id}/vlm_review`.

**Langfuse keys in `.env`.** Instrumentation degrades to a no-op when
missing, but you need them to actually *see* the trace. Check:

```bash
grep -E "^LANGFUSE_" api/.env
```

Should show `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, and
optionally `LANGFUSE_BASE_URL` (defaults to
`https://cloud.langfuse.com`).

---

## Step 1 — Confirm the VLM backend is healthy

```bash
curl -s -X POST http://localhost:8000/api/v1/vlm/health
```

**Expected output:**

```json
{
  "ok": true,
  "backend": "ollama",
  "enabled": true,
  "model": "qwen3-vl:4b",
  "latency_ms": 17141,
  "reason": null
}
```

**First call is slow** — the model cold-loads into memory. Subsequent
calls are much faster (~2-5s on the same box). Expect 15-30s on the
GTX 1660.

**If `ok: false`:**

| `reason` says | Fix |
|---|---|
| "Ollama not reachable at ..." | Start the Ollama app (or `ollama serve`) |
| "no VLM model pulled" | `ollama pull qwen3-vl:4b` |
| "VLM_ENABLED=false" | Flip it in `api/.env` |
| "model responded with error: ..." | Model failed to load — likely VRAM OOM; try `VLM_MODEL_PRIMARY=qwen3-vl:2b` |

---

## Step 2 — Pick a scrim VOD to test on

You want a VOD long enough to produce candidates from multiple sources
(so the per-clip loop actually has something to validate). A League
scrim of 30-60 min is ideal because the Riot API will produce solid
ground-truth kill candidates the VLM can confirm.

From the Sources gallery (`http://localhost:5173/assets`), pick one
and note its asset id (the short hash on the tile — 8 chars is enough).

Or from the terminal:

```bash
curl -s http://localhost:8000/api/v1/assets \
  | python -c "import sys,json; [print(a['id'][:8], a['filename']) for a in json.load(sys.stdin) if (a.get('duration_seconds') or 0) > 1800][:5]"
```

---

## Step 3 — Trigger a compile with VLM validation

Two paths — pick one:

### Path A — via MCP (conversational)

Ask Claude / your MCP client:

> "Compile a highlight reel from asset `<id>` with VLM review on."

The MCP `finalize_compilation` tool will do it end-to-end.

### Path B — via HTTP directly

```bash
# 1) Generate candidates + rank + compile in one shot
curl -s -X POST http://localhost:8000/api/v1/assets/<ASSET-ID>/analyze
# Wait ~30-60s depending on candidate count; the job returns when done

# 2) Trigger a compile with vlm_review on
curl -s -X POST "http://localhost:8000/api/v1/assets/<ASSET-ID>/compile"
```

Watch the API logs — you should see log lines for each VLM iteration
starting to fire once the per-clip loop kicks in
(`vlm-validate-clip`, `vlm-per-clip-iter`).

**Expected compile time impact on GTX 1660** with ~10 kept clips:
+3-8 minutes vs the pre-VLM baseline.

---

## Step 4 — Inspect the Langfuse trace

Open [https://cloud.langfuse.com](https://cloud.langfuse.com) →
select your project → **Traces**. Find the newest one; name will be
`compile_asset:<recording-stem>`.

### What the tree should look like

```
compile_asset:...
├─ rank-highlight-candidates    (existing Anthropic ranker, ~2s)
├─ (initial compile step)
└─ vlm_review [tag: vlm, backend: ollama, model: qwen3-vl:4b]
   ├─ vlm-validate-clip clip:01
   │  ├─ vlm-per-clip-iter (n=1) → verdict=fixable, fix=extend_before 2s
   │  ├─ vlm-per-clip-iter (n=2) → verdict=pass ✓
   ├─ vlm-validate-clip clip:02
   │  └─ vlm-per-clip-iter (n=1) → verdict=pass ✓
   ...
   └─ vlm-whole-comp-pass (if you triggered a review)
```

### Clicking a span shows

- **Input** — the exact user prompt (event context, clip length)
- **Output** — the parsed verdict JSON
- **Metadata** — `backend`, `hints_file`, `frames` count, active
  model tier
- **Tags** — `vlm`, `ollama`, `per-clip` or `whole-comp`
- **Latency** — one span's wall clock, which is your real per-call cost

### Reading the trace (what to look for)

- **`false_positive` verdicts** — every one is a candidate the finder
  got wrong. Find the pattern (Are all from `audio_peak`? Only in a
  certain HUD state?) → that's a finder-tuning signal.
- **Multiple iterations on a single clip** — those clips are the ones
  the VLM kept nudging (extend/trim). Great signal for adjusting the
  finder's default `pre`/`post` windows.
- **`hints_file: "_default"`** — the game wasn't recognized. Either
  the asset's `game` field isn't set (check the asset row) or the
  filename didn't parse. Add a specific `<game>.md` file if this
  happens on a game you care about.
- **Long latency spikes** — if one call takes 60s+ and others are
  ~5s, that's usually VRAM pressure. Consider dropping
  `VLM_FRAME_SAMPLES_CLIP` from 8 → 4.

---

## Step 5 — Try the whole-comp review

After a compile has finished + rendered `compilation.mp4`:

### Via the webapp

Open the compilation in the viewer (`http://localhost:5173/comp/<id>`).
The right sidebar has a **VLM taste layer** panel:

- If Ollama is reachable and the model is loaded, the "review with VLM"
  button is enabled
- Click it → suggested fixes appear below the button, each with a
  `clip_ref`, one-line `issue`, and the fix type

### Via curl

```bash
curl -s -X POST http://localhost:8000/api/v1/edit/compile/<COMP-ID>/vlm_review
```

**Expected output:**

```json
{
  "ok": true,
  "passes": 1,
  "is_cohesive": false,
  "fixes": [
    {"clip_ref": "03", "issue": "same event_type as clip 02", "fix": "remove_clip", "fix_seconds": null, "roi": null, "focus_x": null, "focus_y": null},
    {"clip_ref": "07", "issue": "much longer than other clips", "fix": "trim_end", "fix_seconds": 3.0, "roi": null, "focus_x": null, "focus_y": null}
  ],
  "backend": "ollama",
  "model": "qwen3-vl:4b"
}
```

### Via MCP

Ask Claude:

> "Run vlm_review_compilation on compilation `<id>`."

The trace shows up as a new top-level `vlm-whole-comp-loop` span with
one `vlm-whole-comp-pass` child (since v1 is single-pass review-only).

---

## Common issues

### The health call hangs > 60s

That's the cold model load on first hit. Expected on GTX 1660 with
`qwen3-vl:4b` — takes ~15-20s. If it hangs longer than 90s the model
probably failed to load (usually VRAM); check the Ollama app's terminal
window for errors, or try `VLM_MODEL_PRIMARY=qwen3-vl:2b` in `.env`
and restart the API.

### Trace shows no VLM spans

Two common causes:

1. **Langfuse keys empty** — instrumentation is optional and no-op's
   when keys aren't set. `grep LANGFUSE api/.env` should show real
   values.
2. **`VLM_ENABLED=false`** — the per-clip loop is skipped entirely,
   so no VLM calls fire. Flip to `true`, restart the API.

### Compile takes way longer than expected

Every per-clip iteration is a full VLM call. If most of your clips
need 3-5 iterations, you're paying that latency 3-5x per clip.
Check the trace: if the verdicts are wandering (extend / trim /
extend / trim), the prompt's `game_hints/<game>.md` might be too
vague. Sharpen it — the loop's behavior tracks the prompt directly.

### `false_positive` verdicts on clips that ARE the event

The VLM is being too strict. Two levers:

- Increase `VLM_FRAME_SAMPLES_CLIP` (default 8) so the model has more
  visual evidence to work with
- Loosen the `false_positive` definition in the system prompt
  (`vlm/prompts.py::_CLIP_SYSTEM_TEMPLATE`)

### Whole-comp review returns empty fixes even when the reel is bad

The prompt asks the model to return `is_cohesive: true` when there
are no fixes — the model may be over-eager to please. Tune the
system prompt in `_COMP_SYSTEM_TEMPLATE` to demand more scrutiny.

---

## Testing without Ollama (offline mode)

The whole VLM module is designed to no-op cleanly:

1. Stop Ollama (or disable the model temporarily)
2. Trigger a compile — every clip lands with the `pass` verdict and
   the `skip_vlm_unavailable` reason
3. Trace tree still shows the outer spans, but each has an
   `output.verdict: pass` with `why: skip_vlm_unavailable: ...`

This means you can develop / run tests / demo the pipeline on a box
without a GPU at all. Ollama is purely an enrichment layer.

---

## Automated test suite

67 unit tests cover the VLM module — no Ollama or ffmpeg needed:

```bash
cd api
.venv/Scripts/python.exe -m pytest tests/test_vlm_*.py -v
```

They cover:

- Verdict schema validation (bounds, enums, required fields per
  verdict type)
- Game-hints file resolution (specific → default → missing)
- Prompt template shape
- Backend retry logic (bad JSON recovers, unavailable doesn't retry)
- Loop mechanics (`pass` exits, `false_positive` skips, `fixable`
  iterates, cap reached keeps the last cut)
- Window fix math (extend/trim bounded by 0 and duration)

The full suite (`.venv/Scripts/python.exe -m pytest -q`) is 452 tests
including the VLM ones, all green as of shipping.
