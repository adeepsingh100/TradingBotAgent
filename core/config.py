"""Static, secret, infra-level config -- env-sourced, read once at
process start, never changed without a redeploy.

Behavioral knobs that the dashboard's Controls & Settings page needs to
change without a redeploy (risk params, fee rates, wallets, watchlist,
LLM provider/model) are NOT here -- they live in the `settings` table
(core/db/models.py::Setting), seeded with the DEFAULT_SETTINGS below by
scripts/seed_wallets.py, and read fresh by the worker every tick (spec
section 3: "Reads control_commands and settings each tick"). This file
is the one split every other tunable in this repo follows.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Database ---
    database_url: str

    # --- Worker auth ---
    # Defaults empty, not required -- worker-only (worker/app.py's POST /tick
    # auth check), but this Settings class is shared with app/ (the
    # dashboard), which never reads tick_token and whose secrets.toml
    # deliberately doesn't include it (see app/.streamlit/secrets.toml.example).
    # A required-with-no-default field here would make Settings() itself
    # unconstructable for the dashboard. The worker's own deployment is what
    # actually enforces a real value (render.yaml's TICK_TOKEN has no
    # default -- Render prompts for it), not Pydantic required-ness.
    tick_token: str = ""

    # --- Exit guard (worker/exit_guard.py) -- seconds between stop/target
    # passes in the worker process; 0 disables. Process-level (a thread
    # started at boot), so env, not the settings table.
    exit_guard_seconds: float = 10.0

    # --- LLM provider factory defaults (overridable via the `settings`
    # table's llm_provider/llm_model keys -- these are just what a fresh
    # DB gets seeded with) ---
    llm_provider: str = "nvidia"
    llm_model: str = "nvidia/nemotron-3-ultra-550b-a55b"
    nvidia_api_key: str = ""
    anthropic_api_key: str = ""
    openai_api_key: str = ""

    # --- CoinDCX ---
    coindcx_api_key: str = ""
    coindcx_api_secret: str = ""

    # --- Firebase Auth ---
    firebase_web_api_key: str = ""
    firebase_service_account_json: str = ""
    allowed_emails: str = ""  # comma-separated; see allowed_emails_list

    # --- Telegram ---
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # --- Wallet/risk seed defaults (used only by seed_wallets.py; live
    # values after that come from the `settings` table) ---
    wallet_small_starting_capital: float = 10000.0
    wallet_large_starting_capital: float = 10000.0
    death_threshold_pct: float = 20.0

    @property
    def allowed_emails_list(self) -> list[str]:
        return [e.strip().lower() for e in self.allowed_emails.split(",") if e.strip()]


settings = Settings()
