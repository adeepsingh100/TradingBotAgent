"""Provider factory + structured-call wrapper carrying this project's
"any LLM error -> HOLD" rule: every call site gets `None` back on ANY
failure (bad response, timeout, rate limit, provider outage) instead
of an exception, and an `llm_calls` row either way.

ChatNVIDIA facts verified live against the real API while building
this (package `langchain-nvidia-ai-endpoints==1.4.3`): constructor
takes `max_completion_tokens`, not `max_tokens`; client timeout is a
hardcoded ~30s with no constructor override; no typed rate-limit
exception exists to special-case, hence the blanket `except Exception`
below -- that's deliberate, not sloppy.
"""

from __future__ import annotations

import time
from typing import TypeVar

from pydantic import BaseModel

from core.db.models import LLMCall

_T = TypeVar("_T", bound=BaseModel)


def get_llm(provider: str, model: str, api_keys: dict[str, str]):
    """`anthropic`/`openai` branches wired and dependency-pinned in
    Phase 8 but NOT live-verified against a real completion -- both
    API keys in .env are empty. What IS confirmed: both raise a clean,
    specific credentials error with no key available, never a generic
    crash -- but at different points. ChatOpenAI validates eagerly
    INSIDE this function (its constructor calls the openai SDK's own
    client init, which checks for a key immediately) -- a caller must
    wrap get_llm() itself in try/except, which worker/cycle.py already
    does. ChatAnthropic defers validation to call time instead, so its
    failure surfaces inside call_structured()'s own try/except as an
    ordinary failed LLMCall row, same as every other provider failure.
    Confirm an actual successful completion for both before depending
    on either in a live flow."""
    if provider == "nvidia":
        from langchain_nvidia_ai_endpoints import ChatNVIDIA

        return ChatNVIDIA(model=model, nvidia_api_key=api_keys["nvidia_api_key"], max_completion_tokens=1024)
    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(model=model, api_key=api_keys["anthropic_api_key"], max_tokens=1024)
    if provider == "openai":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(model=model, api_key=api_keys["openai_api_key"], max_tokens=1024)
    raise ValueError(f"unknown llm provider {provider!r}")


def call_structured(
    session,
    llm,
    schema: type[_T],
    messages: list[dict],
    *,
    node: str,
    wallet_id,
    provider: str,
    model: str,
) -> _T | None:
    start = time.monotonic()
    try:
        result = llm.with_structured_output(schema).invoke(messages)
        latency_ms = int((time.monotonic() - start) * 1000)
        session.add(
            LLMCall(wallet_id=wallet_id, node=node, provider=provider, model=model, latency_ms=latency_ms, success=True)
        )
        return result
    except Exception as exc:  # noqa: BLE001 -- intentional: any LLM failure degrades to HOLD, never a crash
        latency_ms = int((time.monotonic() - start) * 1000)
        session.add(
            LLMCall(
                wallet_id=wallet_id, node=node, provider=provider, model=model,
                latency_ms=latency_ms, success=False, error=str(exc)[:2000],
            )
        )
        return None
