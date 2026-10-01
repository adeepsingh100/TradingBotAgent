"""Regression test for a real bug hit live (Phase 6 report): touching
`st.secrets` at all when no secrets.toml exists makes Streamlit render
a red error banner as a side effect of the access itself, which
catching the resulting exception afterward can't undo -- the fix is to
check the file exists first and never touch `st.secrets` otherwise."""

from __future__ import annotations

import os

from app.lib import bootstrap


def test_ensure_env_from_secrets_no_ops_when_no_secrets_file_exists(monkeypatch):
    monkeypatch.setattr(bootstrap, "_SECRETS_PATHS", ())  # simulate neither path existing
    monkeypatch.delenv("SOME_TEST_KEY", raising=False)

    bootstrap.ensure_env_from_secrets()  # must not raise or import streamlit

    assert "SOME_TEST_KEY" not in os.environ
