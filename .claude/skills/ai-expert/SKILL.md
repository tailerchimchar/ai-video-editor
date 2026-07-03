# AI Expert

---
name: ai-expert
description: Project's AI/LLM expertise — the candidate-first architecture, the single Anthropic call, model choice, prompt caching, the process-level spend guard, Langfuse tracing, and why there is intentionally no RAG yet. Use when adding LLM calls, tuning the ranker, debugging traces, reasoning about token cost, or answering "should we use an LLM for X".
---

## Central rule: exactly ONE Anthropic call in the whole app

There is precisely one place this app talks to Anthropic:
`api/src/ai_video_editor/ranker.py:144` — `client.messages.parse()`,
invoked once per video from the rank job in
`api/src/ai_video_editor/routers/analyze.py:187` via
`asyncio.to_thread(rank_candidates, ...)`.

Nothing else calls Anthropic. Not candidate generation. Not scan.
Not transcript. Not compile. Not editing primitives. Not intros. Not
splits. Not the MCP layer. The default answer to "should we add an
LLM call for X" is **no** — every addition is a per-video cost + a
new failure mode + a latency spike, and this app has succeeded so far
by keeping that surface at exactly one call.

## The candidate-first architecture (why the LLM never watches video)

From `api/CLAUDE.md`:

> generate ~hundreds of *cheap* candidate moments from deterministic
> signals, then use one small LLM call to *reduce and rank* them.
> The LLM never watches video; it ranks structured metadata. This
> is ~100× cheaper than vision and fully traceable.

Signals that produce candidates (`api/src/ai_video_editor/candidates/service.py`):

- `outplayed_clip` — short files are already Outplayed event clips
- `audio_peak` — RMS energy peaks (`candidates/audio.py`)
- `riot_api` — MATCH-V5 `CHAMPION_KILL` events (League only, via `league/`)
- `cv_kda` — OCR on the in-game scoreboard (`candidates/cv_kda.py`)
- `transcript_keyword` — Whisper STT + keyword rules

Every one of these produces `HighlightCandidate` rows deterministically
and locally, with `source`, `start_seconds`, `end_seconds`,
`event_type`, `confidence`, and a `metadata` JSON blob
(`api/src/ai_video_editor/database.py` `highlight_candidates` table).
The rank job hands the full batch — as JSON — to Claude.

The LLM sees text. The LLM outputs text. Video never enters the
prompt. This is the entire cost model of the app.

## Model choice — Haiku by default, and mean it

`api/src/ai_video_editor/config.py:31`:

```python
ranker_model: str = "claude-haiku-4-5"
```

Ranking structured metadata is an extraction/scoring task, not a
reasoning task. Haiku 4.5 clears it. `api/docs/cost-and-tracing.md`
gives the numbers:

| Model | Cost per typical rank |
|---|---|
| `claude-haiku-4-5` (default) | ~$0.005 |
| `claude-opus-4-7` | ~$0.05 |

Verified on a real 34-candidate batch: 8.4k input / 3.6k output on
`claude-haiku-4-5` ≈ $0.026. Bigger batches skew above the $0.005
average — the "typical" number is for smaller lists.

**When to consider upgrading**: probably never for the ranker. If
someone opens the door to Sonnet 4.6 or Opus 4.8, require them to
show a specific ranking failure Haiku produces that a bigger model
demonstrably fixes on the same input. "Feels smarter" is not a
reason. Every 10x model tier costs 10x per video, forever.

Latest ids the harness knows (for reference when *not* the ranker):
- `claude-fable-5`
- `claude-opus-4-8`
- `claude-sonnet-4-6`
- `claude-haiku-4-5-20251001` (long id — `ranker.py` uses the short
  alias `claude-haiku-4-5`)

## The `messages.parse` pattern

`api/src/ai_video_editor/ranker.py:98-100, 155`:

```python
class _RankerOutput(BaseModel):
    results: list[RankedCandidate]

response = client.messages.parse(
    model=settings.ranker_model,
    max_tokens=8000,
    system=[{"type": "text", "text": _SYSTEM_PROMPT,
             "cache_control": {"type": "ephemeral"}}],
    messages=[{"role": "user", "content": payload}],
    output_format=_RankerOutput,
)
results = response.parsed_output.results
```

Key facts about `messages.parse`:

1. It's the SDK's Pydantic-typed structured-output convenience. Under
   the hood, it forces the model to emit JSON matching the schema.
   No parsing code needed — you read `response.parsed_output`.
2. **`AnthropicInstrumentor` does NOT wrap `messages.parse`.** This
   is the crucial gotcha. If you rely on the auto-instrumenter to
   capture tokens/cost, `parse` calls trace as $0 in Langfuse. The
   ranker works around this by attaching usage manually
   (`ranker.py:163-183`, more below).
3. `max_tokens=8000` is a headroom cap for the ranked results; a
   50-candidate batch usually fits in ~3-4k output.

If you add a second LLM call — don't — and you use anything other
than `parse`, keep the same manual-usage attach pattern OR add
Langfuse tracking with the SDK's plain `messages.create` (which
IS instrumented).

## Prompt caching — frozen system, volatile user

`ranker.py:39-95` is a **frozen** system prompt — no timestamps, no
per-request substitution, no IDs. That's deliberate. It forms a
stable cacheable prefix, marked with `cache_control: ephemeral`
(`ranker.py:151`). Anthropic's ephemeral cache holds for 5 minutes;
back-to-back rank calls hit the cache on the second call onward.

Volatile candidate JSON lives in the user turn, after the breakpoint
(`ranker.py:130-134, 154`):

```python
payload = json.dumps(
    {"game": game, "candidates": candidates},
    sort_keys=True,
    default=str,
)
```

`sort_keys=True` is important: candidate JSON must serialize
deterministically or otherwise-identical requests miss the cache.

**Caveat from `api/docs/cost-and-tracing.md`:** the ranker's system
prompt (~1-2k tokens today) may sit BELOW the model's minimum
cacheable-block size, in which case caching silently no-ops. This is
harmless — Anthropic just processes the request without cache credit.
Do not "pad" the system prompt to force caching — the writing quality
matters more than the cache hit.

## Spend guard — refuses at N+1, no HTTP call made

`ranker.py:105-126`:

```python
_rank_call_count = 0

class SpendCapError(RuntimeError):
    ...

def rank_candidates(game, candidates):
    global _rank_call_count
    if _rank_call_count >= settings.anthropic_max_rank_calls:
        raise SpendCapError(
            f"rank-call cap reached ({settings.anthropic_max_rank_calls} per "
            f"server run) — raise ANTHROPIC_MAX_RANK_CALLS or restart to reset. "
            f"No Anthropic call was made."
        )
    _rank_call_count += 1
    ...
```

- Process-level counter — resets when uvicorn restarts. That IS the
  intended granularity: a fresh process = a fresh budget.
- Default cap: `ANTHROPIC_MAX_RANK_CALLS = 25` (`config.py:34`).
- N+1 raises **before** the HTTP call. `SpendCapError` propagates
  through `asyncio.to_thread` and lands as a failed job with a clear
  error message.

**When to raise the cap**: batch analysis of a backlog (e.g. ranking
100 recordings sequentially). Set it high (`ANTHROPIC_MAX_RANK_CALLS=200`
in `.env` or via env var), run the batch, restart to reset. **Never
remove the guard.** It exists specifically to catch a runaway loop
that would otherwise drain the Anthropic balance overnight.

## Bounded client — no infinite retry, fail fast on 4xx

`ranker.py:139-143`:

```python
client = anthropic.Anthropic(
    api_key=settings.anthropic_api_key,
    max_retries=settings.ranker_max_retries,      # default 2
    timeout=settings.ranker_timeout_seconds,       # default 120s
)
```

- 429/5xx get exponential-backoff retried by the SDK, capped at 2
  attempts.
- 4xx (billing, malformed request, quota exceeded on billing side)
  are NOT retried — they fail fast so you see the real error.
- 120-second timeout so a single hung request can't sit forever.

If you add another LLM call, mirror these bounds. `client.Anthropic(api_key=...)`
with defaults is a footgun — the SDK's default retries can loop for
minutes on a broken key.

## Langfuse tracing

Setup lives in `api/src/ai_video_editor/tracing.py`. Two things
matter:

1. `AnthropicInstrumentor` auto-captures traces for
   `messages.create` calls. It does NOT capture `messages.parse`.
   (This is why the ranker attaches usage manually — see next
   section.)
2. Credentials are pushed into `os.environ` BEFORE the Langfuse
   client initializes (`tracing.py:32-34`). Order matters — Langfuse
   reads env at init time.

Required env vars (`.env`):

```
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_BASE_URL=https://cloud.langfuse.com    # override for self-host
```

If keys are absent, `init_tracing()` returns early (`tracing.py:29-30`)
and the app runs without tracing. Never fatal.

**When to flush**: `flush_tracing()` should be called on API
shutdown. `main.py`'s lifespan already does this. Long-running
background jobs (compile, transcribe) finish between requests, so
without a flush their spans can be lost when uvicorn restarts.

There is already a scoped `langfuse` skill at
`api/.claude/skills/langfuse` that covers CLI usage against the
Langfuse API, log search, and the SDK. If you need to query traces
or programmatically manage prompts/datasets/scores, invoke that skill
— don't reinvent.

## The manual usage-attach dance (because `parse` isn't instrumented)

`ranker.py:163-183`:

```python
lf = get_client()
if lf is not None:
    with contextlib.suppress(Exception):
        u = response.usage
        usage_details = {"input": u.input_tokens, "output": u.output_tokens}
        for attr in ("cache_read_input_tokens", "cache_creation_input_tokens"):
            tokens = getattr(u, attr, None)
            if tokens:
                usage_details[attr] = tokens
        lf.update_current_generation(
            model=response.model,
            usage_details=usage_details,
        )
        lf.update_current_trace(
            input={"game": game, "num_candidates": len(candidates)},
            output={"kept": sum(1 for r in results if r.keep)},
            tags=["highlight-ranker"],
        )
```

Rules:

- Wrap in `contextlib.suppress(Exception)` — tracing must never take
  down the API.
- Attach `model=response.model` (the actual model id echoed back —
  not `settings.ranker_model`, in case someone routed via a proxy or
  used a different alias).
- Pass cache tokens when present — Langfuse uses these to compute
  the real (post-cache) input cost.
- Set trace input/output EXPLICITLY. Don't let raw args leak into
  the trace — you don't want the whole candidate JSON as the "input"
  field. Summarize.
- Tag with `"highlight-ranker"` for filterability.

**Historical note from `api/docs/cost-and-tracing.md`:** rank calls
made BEFORE the manual-attach fix show $0 / 0 tokens in Langfuse.
For historical spend, the Anthropic Console → Usage view is
authoritative. New calls are accurate.

## `@observe` decoration

`ranker.py:112`:

```python
@observe(name="rank-highlight-candidates", as_type="generation")
def rank_candidates(game, candidates):
    ...
```

- `as_type="generation"` marks this as an LLM generation in Langfuse
  (vs a plain span), which enables the model/usage/cost columns.
- Named explicitly so the trace list stays searchable.
- The `observe` decorator is soft-imported (`ranker.py:25-33`) so
  `langfuse` remains an optional dependency — the code still runs
  without Langfuse installed.

## The system-prompt is the model's context — treat it as taste

`ranker.py:50-95`. This is not filler — it encodes editing taste per
the user's reference channels (Zekken / JawGemo / PewDiePie) and
weights the candidate sources strongest-first (`riot_api > cv_kda >
outplayed_clip > transcript_keyword > audio_peak`). Changing this
prompt directly changes the taste of the reels.

Rules for editing:

- Keep it FROZEN. No per-request substitution, no dates, no IDs.
  It's the cacheable prefix.
- If you tune it, test on a real ranked batch and eyeball the
  before/after `kept` set. Small prompt tweaks can flip 20% of
  keep/reject decisions.
- Cross-source pairing (transcript within ~15s of a kill) is called
  out explicitly in the prompt — this is the "gold pattern" the
  reels lean on. Don't remove it lightly.

## No RAG yet — and why

From `api/CLAUDE.md`:

> Today there is **no RAG and no reranker** — and that's correct,
> because there's no text corpus to embed yet. The ranker
> (`ranker.py`) is a *scoring* step, not a reranker.

RAG becomes meaningful only after the `transcript_keyword` source
matures into a full corpus: Whisper STT running over full sessions,
segments embedded into `sqlite-vec` (already scaffolded — see
`config.py:70-75` for the `bge-small` model + 384-dim + 25s chunk
window), and semantic queries like "find my clutch 1v3" going
through vector search + reranker.

The current `ranker.py` is a **scoring** step, not a **reranker**.
Don't call it a reranker. A reranker only shows up in the RAG
pipeline, reordering retrieved results — that's a separate future
component. Conflating the two makes designs muddled.

Embeddings infra is already partially built (`sqlite-vec` extension
loaded at `database.py`, `embeddings` vec0 virtual table), so when
transcripts arrive at volume, the RAG path can slot in without a
schema change.

## Cost model summary

| Action | Anthropic call? | Cost |
|---|---|---|
| `POST /assets/scan` | no | $0 |
| `POST /assets/{id}/candidates` | no | $0 |
| `POST /assets/{id}/transcribe` (Whisper local) | no | $0 |
| `POST /assets/{id}/rank` | yes — 1 `messages.parse` | ~$0.005 (default) |
| `POST /assets/{id}/highlights` | no | $0 |
| `POST /compile/...` | no | $0 |
| All editing endpoints | no | $0 |
| MCP tools | no (they call HTTP; only rank hits Anthropic) | $0 unless rank tool invoked |

Nothing loops. Nothing retries beyond the SDK's bounded 2 attempts.
The spend cap catches runaway loops.

## Extending the AI surface

Before you propose adding a new LLM call, ask:

1. Can this be done deterministically? Grep, regex, a heuristic, a
   ffprobe read, a curl to an API? Nine times out of ten the answer
   is yes and the deterministic path is faster + free.
2. Can this be a Whisper output post-processed with rules?
3. Can this be a Pydantic-validated LLM output from an *existing*
   call by extending the schema?

If you must add one:

- Read a validated key from `settings.anthropic_api_key`.
- Reuse the bounded-client pattern (`max_retries`, `timeout`).
- Add a NEW spend guard counter for the new endpoint. Do NOT share
  `_rank_call_count` — the two limits should be independently
  tunable.
- Decorate with `@observe(name=..., as_type="generation")`.
- If you use `messages.parse`, mirror the manual usage-attach block.
  If you use `messages.create`, `AnthropicInstrumentor` covers it
  automatically.
- Add the call to `api/docs/cost-and-tracing.md` in the "When do we
  call Anthropic?" table.
- Add a per-video cost estimate for it.
- Default the model to Haiku unless there's a demonstrated failure
  Haiku makes that a bigger model demonstrably fixes on real inputs.

## Anti-patterns

- **Mocking the Anthropic client in tests.** Ranker tests either
  run against a real key with the smallest possible input (mock only
  network at the transport level, if at all), or bypass the LLM by
  testing pure helpers (`_RankerOutput`, JSON serialization,
  `SpendCapError` triggering). A mocked `messages.parse` that "just
  returns some JSON" tests nothing about how the system will behave
  under real model output.
- **Sending video frames to the LLM.** Vision is banned by design.
  If you find yourself reaching for `image` content blocks, stop —
  the CV-based candidate sources (`cv_kda`) are the right layer for
  frame reads.
- **Unbounded loops without a spend guard.** Anything that iterates
  and calls `rank_candidates` (or any future LLM function) inside a
  loop must have its own N-per-process counter with clear-error
  refusal, mirroring `SpendCapError`.
- **A second LLM call in the same job path.** Two calls per video =
  2x the cost, 2x the latency, 2x the failure surface, and the
  spend-cap math no longer matches reality. Fold work into the
  existing ranker if you can. If you cannot, it's a new pipeline
  stage with its own guard.
- **"Let me just use Sonnet 4.6 to see if it's better."** Nope. Do
  the before/after eval on a fixed candidate batch and report kept
  set diffs. Model tier changes at 10x cost cannot be committed on
  a vibe.
- **`shell=True` in ANY context.** Not AI-specific, but flagged by
  `api/CLAUDE.md` conventions. All subprocess invocations use argv
  lists built from validated Pydantic inputs. This applies equally
  to any FUTURE LLM helper that shells out (it shouldn't).
- **`.env` in git.** `ANTHROPIC_API_KEY`, `RIOT_API_KEY`,
  `LANGFUSE_*_KEY` — all live in `api/.env`, which is gitignored.
  Never echo secrets in logs, traces, or error messages.
- **Leaking raw args into the trace.** `update_current_trace(input=..., output=...)`
  is called with a SUMMARY, not the raw candidate JSON. A Langfuse
  trace with 8k tokens of candidate metadata as the "input" field
  is unreadable and expensive to store.
- **Skipping `contextlib.suppress` around tracing.** Any exception
  in Langfuse code must be swallowed so the API never fails because
  the trace broke.
- **Editing the frozen system prompt without eval.** The prompt is
  the taste of the reels. Change it, run rank on a known video,
  diff the `kept` set. If the diff is worse, revert.

## Debugging playbook

- **Job fails with "rank-call cap reached"** — process hit the
  25-per-run limit. Restart uvicorn (`uv run dev`) to reset the
  counter, or bump `ANTHROPIC_MAX_RANK_CALLS` in `.env`. This is
  the guard working correctly; it means the app was about to make
  its 26th call. Confirm nothing runaway is happening.
- **Job fails with 401 / 403** — bad or expired `ANTHROPIC_API_KEY`.
  The bounded client fails fast on 4xx (no retry loop). Check `.env`.
- **Job fails with 400 billing error** — the account is out of
  credits or hit a rate limit. Fail-fast is correct here; do not
  wrap in retry logic.
- **Langfuse shows $0** — trace was from before the manual
  usage-attach was added, OR the account has no cost pricing
  configured. Anthropic Console → Usage remains authoritative for
  historical spend.
- **Ranker returns empty `results`** — the candidate list was
  empty (see `ranker.py:116-117`). Root cause is upstream: run
  `POST /assets/{id}/candidates` first and inspect the
  `highlight_candidates` table.
- **Ranker is slow** — check the batch size. A 100-candidate rank
  can take 20-30s on Haiku. Timeout is 120s. If it's hitting the
  timeout, break the video into a shorter window and re-rank, or
  tighten the candidate generators to emit fewer noisy candidates.

## Files this skill trusts

- `api/src/ai_video_editor/ranker.py` — the ONE Anthropic call, and
  the source of truth for the ranker pattern.
- `api/src/ai_video_editor/tracing.py` — Langfuse init + flush.
- `api/src/ai_video_editor/config.py` — all AI settings (model,
  spend cap, retries, timeout, Langfuse credentials).
- `api/src/ai_video_editor/candidates/service.py` — what data the
  LLM receives (structured metadata, never frames).
- `api/src/ai_video_editor/routers/analyze.py` — the `/rank`
  endpoint that dispatches the LLM job.
- `api/CLAUDE.md` — the architecture rationale (candidate-first,
  no RAG yet, cost model).
- `api/docs/cost-and-tracing.md` — verified cost numbers and the
  `messages.parse` instrumentation gotcha.
