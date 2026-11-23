"""The single Anthropic surface: client factory, spend guard, tracing.

Every Anthropic call in the backend (ranker, typed decisions) goes
through these helpers so each call is bounded, counted and traced the
same way.

- Spend guard: process-level call counters, one budget per feature. They
  reset when the server restarts (a fresh process = a fresh budget),
  which stops a stray loop from draining the Anthropic balance.
- Tracing: model + token usage are attached manually because the
  AnthropicInstrumentor does not cover `messages.parse`; without it the
  trace shows $0 / 0 tokens.
"""

import contextlib
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import anthropic

from .config import settings

try:
    from langfuse import get_client, observe
except Exception:  # langfuse optional

    def observe(*_a, **_k):
        def deco(fn):
            return fn

        return deco

    def get_client():  # type: ignore[misc]
        return None


_log = logging.getLogger(__name__)

_calls: dict[str, int] = {}


class SpendCapError(RuntimeError):
    """Raised when a feature's per-process Anthropic call cap is hit."""


def reserve_call(feature: str, cap: int, env_name: str) -> None:
    """Count one call for `feature`, or raise before any API call is made."""
    if _calls.get(feature, 0) >= cap:
        raise SpendCapError(
            f"{feature}-call cap reached ({cap} per server run) — raise "
            f"{env_name} or restart to reset. No Anthropic call was made."
        )
    _calls[feature] = _calls.get(feature, 0) + 1


_LEDGER_DDL = """CREATE TABLE IF NOT EXISTS spend_ledger (
    id INTEGER PRIMARY KEY,
    asset_id TEXT,
    feature TEXT,
    model TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cost_usd REAL,
    ts TEXT
)"""


def _ledger_conn(db_path: Path | None) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path or settings.database_path)
    conn.execute(_LEDGER_DDL)
    return conn


def check_budget(
    asset_id: str | None,
    *,
    db_path: Path | None = None,
    cap_usd: float | None = None,
    cap_name: str | None = None,
) -> None:
    """Raise SpendCapError when this asset's paid calls already cost >= the cap.

    `cap_usd` overrides `settings.cost_cap_per_asset_usd` (e.g. the story
    review budget, keyed `story:<asset_id>`, sits outside the asset cap);
    `cap_name` is the setting the error tells the user to raise.

    None asset_id means no accounting, so no check. Check-then-act is not
    atomic: concurrent calls can overshoot the cap by one request.
    """
    if asset_id is None:
        return
    conn = _ledger_conn(db_path)
    try:
        (total,) = conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM spend_ledger WHERE asset_id = ?",
            (asset_id,),
        ).fetchone()
    finally:
        conn.close()
    cap = settings.cost_cap_per_asset_usd if cap_usd is None else cap_usd
    name = cap_name or ("COST_CAP_PER_ASSET_USD" if cap_usd is None else "the caller's cap")
    if total >= cap:
        raise SpendCapError(
            f"{asset_id} has spent ${total:.2f} (cap ${cap:.2f}, {name}) - "
            f"raise {name}. No Anthropic call was made."
        )


def spent_usd(asset_id: str, *, feature_prefix: str, db_path: Path | None = None) -> float:
    """Sum this asset's ledger cost over features starting with `feature_prefix`."""
    conn = _ledger_conn(db_path)
    try:
        (total,) = conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM spend_ledger "
            "WHERE asset_id = ? AND feature LIKE ? || '%'",
            (asset_id, feature_prefix),
        ).fetchone()
    finally:
        conn.close()
    return float(total)


def record_spend(
    asset_id: str | None,
    feature: str,
    model: str,
    usage,
    *,
    db_path: Path | None = None,
) -> float | None:
    """Write one ledger row and return its cost.

    A model missing from MODEL_PRICES_USD_PER_MTOK is costed conservatively
    at the table's highest input and highest output price (so the per-asset
    cap still trips) and a warning is logged; `model` is recorded as given.
    The cost is None only when the price table is empty.
    Nothing is written when `asset_id` is None.
    """
    if asset_id is None:
        return None
    tokens_in = int(getattr(usage, "input_tokens", 0) or 0)
    tokens_out = int(getattr(usage, "output_tokens", 0) or 0)
    prices = settings.model_prices_usd_per_mtok
    price = prices.get(model)
    if price is None:
        _log.warning(
            "spend ledger: no price for model %r; costing at the most expensive listed price",
            model,
        )
        if prices:
            price = (max(p[0] for p in prices.values()), max(p[1] for p in prices.values()))
    cost = None if price is None else (tokens_in * price[0] + tokens_out * price[1]) / 1_000_000
    conn = _ledger_conn(db_path)
    try:
        with conn:
            conn.execute(
                "INSERT INTO spend_ledger (asset_id, feature, model, input_tokens, "
                "output_tokens, cost_usd, ts) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    asset_id,
                    feature,
                    model,
                    tokens_in,
                    tokens_out,
                    cost,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
    finally:
        conn.close()
    return cost


def make_client() -> anthropic.Anthropic:
    """Bounded: the SDK auto-retries 429/5xx with backoff; cap it and the
    request timeout so a single call can't spin. (4xx like the billing
    400 are not retried at all — they fail fast.)"""
    return anthropic.Anthropic(
        api_key=settings.anthropic_api_key,
        max_retries=settings.ranker_max_retries,
        timeout=settings.ranker_timeout_seconds,
    )


def record_generation(
    response,
    *,
    trace_input: dict,
    trace_output: dict,
    tags: list[str],
    metadata: dict | None = None,
) -> None:
    """Attach model, token usage and explicit trace I/O to the current
    Langfuse generation. Never raises: tracing must not break a call."""
    lf = get_client()
    if lf is None:
        return
    with contextlib.suppress(Exception):
        u = response.usage
        usage_details = {"input": u.input_tokens, "output": u.output_tokens}
        for attr in ("cache_read_input_tokens", "cache_creation_input_tokens"):
            tokens = getattr(u, attr, None)
            if tokens:
                usage_details[attr] = tokens
        extra = {"metadata": metadata} if metadata else {}
        lf.update_current_generation(model=response.model, usage_details=usage_details, **extra)
        lf.update_current_trace(input=trace_input, output=trace_output, tags=tags, **extra)
