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

Found live (llm_calls errors, 2026-10-02): ChatNVIDIA's own
`with_structured_output` tries THREE request formats in sequence
(OpenAI `response_format`, then top-level `guided_json`, then
`nvext.guided_json`) and re-raises only the LAST error. The hosted API
now rejects `guided_json` outright (400 "unknown field"), so every real
failure of the first attempt -- a 429, or a reply cut off at the token
cap -- surfaced as that misleading 400, and burned 3 requests against
the rate limit instead of 1. `_ChatNVIDIA` below sends only the
`response_format` request, which works (verified live), so the real
error is what gets logged. The model (nemotron) spends hidden reasoning
tokens before its JSON (~650 output tokens for a ~100-token Proposal),
so the cap is 4096, not 1024 -- a truncated reply is raised explicitly
rather than silently parsed as None.

Still hit the 4096 cap on ~3% of `strategize` calls (17 of 623 in 12h,
2026-10-03) -- runaway hidden reasoning. Measured live on a real-sized
strategize prompt, 6 samples each: thinking on = 391-831 output tokens
in 3-25s; `chat_template_kwargs={"enable_thinking": False}` = 61-159
tokens in ~1s, no reasoning text, same valid JSON. `think=False` turns
it off per call: used for strategize (picking one of 5 types per pair
needs no deliberation), kept on for decide/research.
"""

from __future__ import annotations

import inspect
import time
from typing import TypeVar

from pydantic import BaseModel

from core.db.models import LLMCall

_T = TypeVar("_T", bound=BaseModel)

_RETRY_DELAYS_S = (2, 5)  # short -- a tick shouldn't stall long on a busy free tier
# 429 rate limit, 503 "Service temporarily overloaded" -- both transient on
# NVIDIA's free tier. A 60s timeout is NOT retried (another 60s per call
# would stall the tick); the fallback model covers that instead.
_RETRYABLE = ("[429]", "[503]")


def _parse_reply(schema: type[_T]):
    def parse(message) -> _T:
        if (message.response_metadata or {}).get("finish_reason") == "length":
            raise ValueError("reply cut off at max_completion_tokens before the JSON finished")
        return schema.model_validate_json(message.content)
    return parse


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
        from langchain_core.runnables import RunnableLambda
        from langchain_nvidia_ai_endpoints import ChatNVIDIA

        class _ChatNVIDIA(ChatNVIDIA):
            def with_structured_output(self, schema, *, think: bool = True, **kwargs):  # see module docstring
                fmt = {"type": "json_schema", "json_schema": {
                    "name": schema.__name__, "schema": schema.model_json_schema(), "strict": True,
                }}
                extra = {} if think else {"chat_template_kwargs": {"enable_thinking": False}}
                return self.bind(response_format=fmt, **extra) | RunnableLambda(_parse_reply(schema))

        return _ChatNVIDIA(model=model, nvidia_api_key=api_keys["nvidia_api_key"], max_completion_tokens=4096)
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
    think: bool = True,
    fallback: tuple | None = None,
) -> _T | None:
    """`fallback` = (llm, provider, model), tried once if the primary
    still fails after its retries. Found live (2026-10-03): on NVIDIA's
    free tier nemotron-3-ultra failed ~24% of calls -- 503 "Service
    temporarily overloaded" and 60s timeouts, spread evenly across every
    10-minute window (random overload, not outages). Each attempt logs
    its own llm_calls row under its own model, so Model Health shows
    which model actually answered."""
    result = _attempt(session, llm, schema, messages, node=node, wallet_id=wallet_id,
                      provider=provider, model=model, think=think)
    if result is None and fallback is not None:
        fb_llm, fb_provider, fb_model = fallback
        result = _attempt(session, fb_llm, schema, messages, node=node, wallet_id=wallet_id,
                          provider=fb_provider, model=fb_model, think=think)
    return result


def _attempt(session, llm, schema, messages, *, node, wallet_id, provider, model, think):
    start = time.monotonic()
    try:
        for delay in (*_RETRY_DELAYS_S, None):
            try:
                # Only our _ChatNVIDIA declares `think`; other clients would forward an
                # unknown kwarg to their API, so they always get the default call.
                supports_think = "think" in inspect.signature(llm.with_structured_output).parameters
                structured = llm.with_structured_output(schema, **({"think": False} if not think and supports_think else {}))
                result = structured.invoke(messages)
                break
            except Exception as exc:  # noqa: BLE001 -- only transient overload is retried, anything else re-raises below
                if delay is None or not any(code in str(exc) for code in _RETRYABLE):
                    raise
                time.sleep(delay)
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
