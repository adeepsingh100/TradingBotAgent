"""Streamlit Community Cloud has no `.env` -- secrets are injected via
`st.secrets` (from the app's Settings -> Secrets panel, same TOML shape
as `.streamlit/secrets.toml.example`). `core.config.settings` is a
module-level singleton built from `os.environ`/`.env` the moment
anything first imports `core.config` -- so this has to run, copying
`st.secrets` into `os.environ`, BEFORE that first import. Every page
in `app/pages/` calls `ensure_env_from_secrets()` right after its own
inline `sys.path` fix (see any page's top few lines) for exactly this
reason -- this function can't do the `sys.path` fix itself, since
`app.lib.bootstrap` isn't importable until the repo root is already on
the path.

Locally (`streamlit run app/Home.py`), there's no secrets.toml and
`st.secrets` access raises -- `core.config.Settings` already reads
`.env` directly via pydantic-settings, so this just no-ops.
"""

from __future__ import annotations

import os
from pathlib import Path

# Same two paths Streamlit itself checks. Touching `st.secrets` at all when
# neither exists makes Streamlit render a big red "No secrets found" error
# banner on the page as a side effect of the access itself -- catching the
# exception afterward doesn't undo that render. Checking first avoids ever
# triggering it during local dev.
_SECRETS_PATHS = (Path.home() / ".streamlit" / "secrets.toml", Path.cwd() / ".streamlit" / "secrets.toml")


def ensure_env_from_secrets() -> None:
    if not any(p.exists() for p in _SECRETS_PATHS):
        return  # local dev -- core.config.Settings reads .env directly instead

    import streamlit as st

    for key, value in st.secrets.items():
        os.environ.setdefault(key, str(value))
