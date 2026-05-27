"""Langfuse tracing setup.

Follows the Langfuse skill's "prefer framework integrations" guidance:
AnthropicInstrumentor auto-captures model name, token usage, and
input/output for every Anthropic SDK call (the baseline requirements),
so we don't hand-instrument LLM calls. The pipeline adds span hierarchy
via @observe.

Credentials are pushed into the env from our pydantic settings *before*
the Langfuse client initializes (the skill's "import/init after env
loaded" rule) and the host is set explicitly so LANGFUSE_BASE_URL vs
LANGFUSE_HOST naming can't bite us.
"""

import contextlib
import os

from .config import settings

_initialized = False


def init_tracing() -> None:
    global _initialized
    if _initialized:
        return
    _initialized = True

    if not (settings.langfuse_public_key and settings.langfuse_secret_key):
        return  # tracing disabled — no keys configured

    os.environ["LANGFUSE_PUBLIC_KEY"] = settings.langfuse_public_key
    os.environ["LANGFUSE_SECRET_KEY"] = settings.langfuse_secret_key
    os.environ["LANGFUSE_HOST"] = settings.langfuse_base_url

    # Tracing must never take down the API.
    with contextlib.suppress(Exception):
        from opentelemetry.instrumentation.anthropic import AnthropicInstrumentor

        AnthropicInstrumentor().instrument()

        from langfuse import get_client

        get_client()  # initialize with the env credentials set above


def flush_tracing() -> None:
    """Flush buffered spans — important since the API process is long-lived
    and background jobs may finish between requests."""
    with contextlib.suppress(Exception):
        from langfuse import get_client

        get_client().flush()
