# Survivor

An autonomous crypto trading agent for CoinDCX (India, INR spot pairs)
that starts with small capital and must survive on it. See `CLAUDE.md`
for architecture and current build-phase status.

**v1 is paper trading only.** No real order ever gets placed yet.

## Setup

### 1. CockroachDB Serverless

Create a free cluster at [cockroachlabs.cloud](https://cockroachlabs.cloud),
grab the connection string from its Connect panel, and change its
suggested `sslmode=verify-full` to `sslmode=require` (verify-full needs
a CA cert file this app's runtimes don't have). Put it in `.env` as
`DATABASE_URL`.

### 2. NVIDIA NIM API key

Create an account at [build.nvidia.com](https://build.nvidia.com),
generate an API key (starts with `nvapi-`), put it in `.env` as
`NVIDIA_API_KEY`.

### 3. CoinDCX API key

Create one in your CoinDCX account settings with **Trade permission
only -- do NOT enable withdrawal permission**. Needed even in v1: order
*creation* is gated behind DRY_RUN in code, but balance/order-status
calls still hit the real private API. Put the key and secret in `.env`.

### 4. Firebase project (dashboard login)

Added in Phase 6 -- not needed yet.

### 5. Telegram bot

Added in Phase 5 -- not needed yet.

### Local install

```bash
# a real Python 3.11+ is required -- `uv` can fetch one without touching
# your system Python:
uv venv --python 3.11 .venv
uv pip install -r requirements.txt

cp .env.example .env   # fill in DATABASE_URL, TICK_TOKEN, NVIDIA_API_KEY, CoinDCX keys

.venv/bin/python -m alembic upgrade head
.venv/bin/python -m scripts.check_db_connection
.venv/bin/python -m scripts.seed_wallets
.venv/bin/python -m pytest
```

### Running locally

Worker: added Phase 5. Dashboard: added Phase 6.

## Deploying

### Worker to Render

Added Phase 5/6 -- `render.yaml` will live at the repo root once the
worker exists.

### cron-job.org

Added Phase 5/6 -- will document the exact URL, `X-Tick-Token` header,
and interval once `/tick` exists.

### Dashboard to Streamlit Community Cloud

Added Phase 6.

## Switching LLM providers

The LLM provider is a factory (`core/llm/provider.py`, added Phase 5)
keyed off the `settings` table's `llm.provider`/`llm.model` (see
`CLAUDE.md`'s config/settings split) -- switching from NVIDIA to
Anthropic or OpenAI is a Settings-page edit plus the matching API key
in `.env`/Render env vars, not a code change.

## Known risks

- **No on-exchange stop-loss for spot, in practice.** The API schema
  allows `stop_limit`/`take_profit_limit` order types, and docs.coindcx.com's
  example response shows them -- but a live query against **all 339**
  real INR spot pairs (2026-10-01) found **zero** that actually offer
  anything beyond `limit_order`/`market_order` (see
  `core/coindcx/client.py`'s module docstring). Spec section 10 asked
  to flag this if it turned out to be true: the live engine (Phase 7+)
  cannot place an exchange-side stop and must simulate one by polling
  and submitting a market/limit sell when the stop level is crossed --
  a real gap window exists between the price crossing the stop and the
  bot's next tick actually firing the sell. Re-check `order_types` on
  the pairs you actually trade before going live; this could change.
- **CoinDCX fee %**: this repo's default fee-rate seed values
  (`scripts/seed_wallets.py::DEFAULT_SETTINGS["costs"]`) are UNVERIFIED
  against an official CoinDCX source -- their fee pages blocked
  automated verification. Confirm your actual tier's maker/taker % from
  your CoinDCX account's own Fees page and correct the `settings` row
  before trusting any paper-trading PnL number.
- **No sandbox/testnet exists for CoinDCX** -- all API testing (even
  read-only balance/order-status calls in DRY_RUN) hits the real
  exchange with a real account.
- **CockroachDB is not Postgres** -- see `CLAUDE.md`'s list of
  dialect gaps already designed around. If something SQL-shaped fails
  mysteriously, check that list before assuming it's a logic bug.
- **`langchain-cockroachdb` (the LangGraph checkpointer package for
  CockroachDB) is young (v0.3.x as of this writing)** -- smoke-test it
  directly in Phase 5 before relying on it; a custom SQLAlchemy-table
  saver is the documented fallback if it misbehaves.
