# Cost & Tracing

## When do we call Anthropic?

| Action | Anthropic call? |
|---|---|
| `scan`, `candidates` (incl. Riot/audio) | **No — $0, all local** |
| `rank` | **Yes — exactly 1 call per video** |

Nothing is automatic and nothing loops. One `POST /assets/{id}/rank` =
one `messages.parse` ranking the entire candidate batch for that video.

## Per-video cost (rough)

Input ≈ frozen system prompt + small candidate JSON (~1 K tokens);
output ≈ ranked results (~1–2 K tokens).

| Model | ~Cost / video |
|---|---|
| `claude-haiku-4-5` (**default**) | ~$0.005 |
| `claude-opus-4-7` | ~$0.05 |

Haiku is the default because ranking structured metadata is an
extraction/scoring task — it doesn't need a frontier model.

## Spend guards

- `ANTHROPIC_MAX_RANK_CALLS` (default 25) — a process-level counter.
  Call N+1 raises `SpendCapError` **before any HTTP call**, surfaced as
  a failed job with a clear message. Resets on server restart (fresh
  process = fresh budget).
- Bounded client: explicit `RANKER_MAX_RETRIES` and
  `RANKER_TIMEOUT_SECONDS`. 4xx (e.g. billing) fail fast — not retried.

## Prompt caching

The ranker's system prompt is a frozen `cache_control: ephemeral`
block; the volatile candidate JSON sits in the user turn after it, so
the cacheable prefix stays stable. (Note: on small system prompts below
the model's minimum cacheable size, caching silently no-ops — harmless.)

## Langfuse tracing

`tracing.py` initializes Langfuse and `AnthropicInstrumentor` at app
startup. The rank pipeline is `@observe(as_type="generation")`, so
**traces** (timing, input/output, `rank-highlight-candidates`) are
captured.

> **Token/cost capture (fixed):** the ranker uses
> `client.messages.parse()`, which `AnthropicInstrumentor` does *not*
> cover. So `ranker.py` attaches model + token usage to the generation
> manually via `update_current_generation(model=..., usage_details=...)`
> from the SDK response's `usage` (incl. cache-read/creation when
> present). Langfuse then computes cost. Verified: a 34-candidate rank =
> 8.4k in / 3.6k out ≈ $0.026 on `claude-haiku-4-5`.
>
> Caveat: rank calls made *before* this fix have no token data and show
> `$0` in Langfuse — for historical spend, **Anthropic Console → Usage**
> remains authoritative. New calls are accurate in Langfuse.

Credentials come from `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` /
`LANGFUSE_BASE_URL`. If keys are absent, tracing silently disables —
it never takes down the API. Spans are flushed on shutdown (the process
is long-lived; background jobs finish between requests).
