"""Mocked HTTP only -- no real network call in the suite (repo
convention). Live verification of signing/endpoints against the real
API happened manually while building client.py (see its module
docstring) and isn't re-run here."""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
import requests

from core.coindcx import client


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.text = text

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            err = requests.HTTPError(response=self)
            raise err


def test_get_signs_nothing_and_hits_public_base(monkeypatch):
    captured = {}

    def fake_get(url, params=None, timeout=10):
        captured["url"] = url
        captured["params"] = params
        return _FakeResponse(json_data=[{"open": 1}])

    monkeypatch.setattr(requests, "get", fake_get)

    result = client.get_candles("I-BTC_INR", "1h", limit=5)

    assert result == [{"open": 1}]
    assert captured["url"] == f"{client.PUBLIC_BASE}/market_data/candles"
    assert captured["params"] == {"pair": "I-BTC_INR", "interval": "1h", "limit": 5}


def test_post_signed_hashes_the_exact_json_bytes_sent(monkeypatch):
    monkeypatch.setattr(client.settings, "coindcx_api_key", "test-key")
    monkeypatch.setattr(client.settings, "coindcx_api_secret", "test-secret")
    captured = {}

    def fake_post(url, data=None, headers=None, timeout=10):
        captured["url"] = url
        captured["data"] = data
        captured["headers"] = headers
        return _FakeResponse(json_data={"ok": True})

    monkeypatch.setattr(requests, "post", fake_post)

    result = client.get_balances()

    assert result == {"ok": True}
    assert captured["url"] == f"{client.API_BASE}/exchange/v1/users/balances"
    assert captured["headers"]["X-AUTH-APIKEY"] == "test-key"

    body = json.loads(captured["data"])
    assert "timestamp" in body
    expected_sig = hmac.new(b"test-secret", captured["data"].encode(), hashlib.sha256).hexdigest()
    assert captured["headers"]["X-AUTH-SIGNATURE"] == expected_sig


def test_retries_on_429_then_succeeds(monkeypatch):
    monkeypatch.setattr(client.time, "sleep", lambda _: None)  # don't actually wait in tests
    calls = {"n": 0}

    def fake_get(url, params=None, timeout=10):
        calls["n"] += 1
        if calls["n"] < 3:
            return _FakeResponse(status_code=429, text="rate limited")
        return _FakeResponse(json_data=[{"ok": True}])

    monkeypatch.setattr(requests, "get", fake_get)

    result = client.get_ticker()

    assert result == [{"ok": True}]
    assert calls["n"] == 3


def test_does_not_retry_a_4xx_client_error(monkeypatch):
    monkeypatch.setattr(client.time, "sleep", lambda _: None)
    calls = {"n": 0}

    def fake_get(url, params=None, timeout=10):
        calls["n"] += 1
        return _FakeResponse(status_code=400, text="bad request")

    monkeypatch.setattr(requests, "get", fake_get)

    with pytest.raises(client.CoinDCXError):
        client.get_ticker()

    assert calls["n"] == 1  # no retry burned on a client-error response


def test_create_order_is_dry_run_by_default_and_never_calls_the_network(monkeypatch):
    def fail_post(*a, **kw):
        raise AssertionError("create_order must not hit the network while ALLOW_LIVE_ORDERS is False")

    monkeypatch.setattr(requests, "post", fail_post)
    assert client.ALLOW_LIVE_ORDERS is False

    result = client.create_order("BTCINR", "buy", "limit_order", 0.001, price_per_unit=8000000)

    assert result["dry_run"] is True
    assert result["would_submit"]["market"] == "BTCINR"


def test_create_order_hits_the_real_endpoint_when_allow_live_is_explicitly_true(monkeypatch):
    monkeypatch.setattr(client.settings, "coindcx_api_key", "k")
    monkeypatch.setattr(client.settings, "coindcx_api_secret", "s")
    captured = {}

    def fake_post(url, data=None, headers=None, timeout=10):
        captured["url"] = url
        return _FakeResponse(json_data={"id": "123", "status": "open"})

    monkeypatch.setattr(requests, "post", fake_post)

    result = client.create_order("BTCINR", "buy", "market_order", 0.001, allow_live=True)

    assert result == {"id": "123", "status": "open"}
    assert captured["url"] == f"{client.API_BASE}/exchange/v1/orders/create"
    assert client.ALLOW_LIVE_ORDERS is False  # the module default is untouched -- only this call opted in
