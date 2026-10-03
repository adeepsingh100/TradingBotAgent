"""No real LLM calls -- `llm` is a tiny stand-in object exposing
`.with_structured_output(schema).invoke(messages)`, matching the one
method call_structured ever calls on it."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from core.llm.provider import call_structured, get_llm


class _Schema(BaseModel):
    value: str


class _FakeStructuredLLM:
    def __init__(self, result=None, exc=None):
        self._result = result
        self._exc = exc

    def invoke(self, messages):
        if self._exc is not None:
            raise self._exc
        return self._result


class _FakeLLM:
    def __init__(self, result=None, exc=None):
        self._result, self._exc = result, exc

    def with_structured_output(self, schema):
        return _FakeStructuredLLM(self._result, self._exc)


class _FakeQuery:
    def all(self):
        return []


class FakeSession:
    def __init__(self):
        self.added = []

    def add(self, obj):
        self.added.append(obj)


def test_get_llm_raises_on_unknown_provider():
    with pytest.raises(ValueError):
        get_llm("made-up-provider", "some-model", {})


def test_call_structured_returns_result_and_logs_a_success_row():
    session = FakeSession()
    llm = _FakeLLM(result=_Schema(value="ok"))

    result = call_structured(session, llm, _Schema, [{"role": "user", "content": "hi"}],
                              node="decide", wallet_id="w1", provider="nvidia", model="m")

    assert result == _Schema(value="ok")
    assert len(session.added) == 1
    assert session.added[0].success is True
    assert session.added[0].node == "decide"


def test_call_structured_returns_none_and_logs_a_failure_row_on_any_exception():
    session = FakeSession()
    llm = _FakeLLM(exc=RuntimeError("provider timeout"))

    result = call_structured(session, llm, _Schema, [{"role": "user", "content": "hi"}],
                              node="strategize", wallet_id="w1", provider="nvidia", model="m")

    assert result is None
    assert len(session.added) == 1
    assert session.added[0].success is False
    assert "provider timeout" in session.added[0].error


class _FlakyLLM:
    """Fails with the given errors in order, then succeeds."""

    def __init__(self, errors, result):
        self._errors, self._result, self.calls = list(errors), result, 0

    def with_structured_output(self, schema):
        return self

    def invoke(self, messages):
        self.calls += 1
        if self._errors:
            raise self._errors.pop(0)
        return self._result


def test_call_structured_retries_a_429_then_succeeds(monkeypatch):
    monkeypatch.setattr("core.llm.provider.time.sleep", lambda s: None)
    session = FakeSession()
    llm = _FlakyLLM([RuntimeError("[429] Too Many Requests")], _Schema(value="ok"))

    result = call_structured(session, llm, _Schema, [], node="decide", wallet_id="w1", provider="nvidia", model="m")

    assert result == _Schema(value="ok")
    assert llm.calls == 2
    assert session.added[0].success is True


def test_call_structured_does_not_retry_a_non_429_error(monkeypatch):
    monkeypatch.setattr("core.llm.provider.time.sleep", lambda s: None)
    session = FakeSession()
    llm = _FlakyLLM([RuntimeError("[400] bad request")], _Schema(value="ok"))

    assert call_structured(session, llm, _Schema, [], node="decide", wallet_id="w1", provider="nvidia", model="m") is None
    assert llm.calls == 1


def test_nvidia_structured_output_raises_on_a_truncated_reply():
    from types import SimpleNamespace

    from core.llm.provider import _parse_reply

    with pytest.raises(ValueError, match="cut off"):
        _parse_reply(_Schema)(SimpleNamespace(response_metadata={"finish_reason": "length"}, content='{"val'))
    ok = SimpleNamespace(response_metadata={"finish_reason": "stop"}, content='{"value": "x"}')
    assert _parse_reply(_Schema)(ok) == _Schema(value="x")


class _ThinkAwareLLM:
    def __init__(self):
        self.think = None

    def with_structured_output(self, schema, *, think: bool = True):
        self.think = think
        return _FakeStructuredLLM(_Schema(value="ok"))


def test_think_false_reaches_a_client_that_supports_it():
    llm = _ThinkAwareLLM()
    call_structured(FakeSession(), llm, _Schema, [], node="strategize", wallet_id="w", provider="nvidia", model="m", think=False)
    assert llm.think is False


def test_think_false_is_not_passed_to_a_client_without_it():
    # _FakeLLM.with_structured_output takes only `schema` -- passing think would TypeError
    result = call_structured(FakeSession(), _FakeLLM(result=_Schema(value="ok")), _Schema, [],
                             node="strategize", wallet_id="w", provider="anthropic", model="m", think=False)
    assert result == _Schema(value="ok")
