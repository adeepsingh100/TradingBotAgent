"""FastAPI TestClient -- run_cycle itself is monkeypatched, so this
only exercises the auth gate and routing, not a real tick."""

from __future__ import annotations

from fastapi.testclient import TestClient

from core.config import settings
from worker import app as app_module


def test_tick_rejects_missing_or_wrong_token(monkeypatch):
    monkeypatch.setattr(settings, "tick_token", "secret-token")
    client = TestClient(app_module.app)

    resp = client.post("/tick")
    assert resp.status_code == 401

    resp = client.post("/tick", headers={"x-tick-token": "wrong"})
    assert resp.status_code == 401


def test_tick_runs_the_cycle_with_the_correct_token(monkeypatch):
    monkeypatch.setattr(settings, "tick_token", "secret-token")
    monkeypatch.setattr(app_module, "run_cycle", lambda holder: {"wallets": []})
    client = TestClient(app_module.app)

    resp = client.post("/tick", headers={"x-tick-token": "secret-token"})

    assert resp.status_code == 200
    assert resp.json() == {"wallets": []}


def test_health_endpoint():
    client = TestClient(app_module.app)
    assert client.get("/health").json() == {"status": "ok"}
