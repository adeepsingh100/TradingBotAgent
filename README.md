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

Create a Firebase project at [console.firebase.google.com](https://console.firebase.google.com),
enable **Authentication -> Sign-in method -> Email/Password**, and add
a user for each dashboard operator. You need two separate credentials:
- **Web API key** (Project settings -> General -> Web API Key) ->
  `.env`'s `FIREBASE_WEB_API_KEY` -- used for the REST sign-in call.
- **Service account JSON** (Project settings -> Service accounts ->
  Generate new private key) -> the whole file's contents, minified to
  one line, as `.env`'s `FIREBASE_SERVICE_ACCOUNT_JSON` -- used by
  `firebase-admin` to verify the ID token server-side. Never commit
  this file or paste its contents anywhere other than `.env`/Streamlit
  Cloud's Secrets panel.

Then set `ALLOWED_EMAILS` to a comma-separated list of the operator
emails who may sign in -- a verified Firebase user whose email isn't
on this list still can't open Controls & Settings (`app/lib/auth.py`).

### 5. Telegram bot

Message [@BotFather](https://t.me/BotFather) to create a bot and get a
token, message your new bot once, then fetch
`https://api.telegram.org/bot<token>/getUpdates` to find your chat id.
Put both in `.env` as `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`. Optional
-- alerts just silently no-op (`core/telegram.py`) if either is blank.

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
.venv/bin/python -m scripts.seed_strategies
.venv/bin/python -m pytest
```

### Running locally

```bash
# FastAPI worker, for hitting /tick manually or with a local cron
.venv/bin/uvicorn worker.app:app --reload
curl -X POST localhost:8000/tick -H "X-Tick-Token: $TICK_TOKEN"

# or skip HTTP entirely -- loops run_cycle() directly, same code path
.venv/bin/python -m worker.run_local
```

```bash
# dashboard
.venv/bin/streamlit run app/Home.py
```

## Deploying

### Worker to Render

`render.yaml` at the repo root is a Blueprint -- in the Render
dashboard: New -> Blueprint -> connect this GitHub repo -> it
auto-detects `render.yaml` and creates the `survivor-worker` web
service with the right build/start commands. You'll be prompted to
fill in the `sync: false` secrets (`DATABASE_URL`, `TICK_TOKEN`,
`NVIDIA_API_KEY`, `COINDCX_API_KEY`, `COINDCX_API_SECRET`,
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`; `ANTHROPIC_API_KEY`/
`OPENAI_API_KEY` only if you're actually using those providers).

No Blueprint -> manual **Web Service**: build command
`pip install -r requirements.txt`, start command
`uvicorn worker.app:app --host 0.0.0.0 --port $PORT`, same env vars.

Render free-tier web services sleep when idle -- that's fine,
cron-job.org's hit on `/tick` wakes it.

### cron-job.org

Create a cron job hitting `POST https://<your-render-app>.onrender.com/tick`
with header `X-Tick-Token: <same value as TICK_TOKEN>`. Interval: spec
section 3's cadence (every 5-15 minutes is reasonable for v1 -- the
tick lock, `core/lock.py`, makes an overlapping/retried call a no-op,
not a double-tick).

### Dashboard to Streamlit Community Cloud

Create an app at [share.streamlit.io](https://share.streamlit.io) pointed
at this repo, main file path `app/Home.py`. In the app's Settings ->
Secrets, paste `app/.streamlit/secrets.toml.example`'s keys filled in
(`DATABASE_URL`, `FIREBASE_WEB_API_KEY`, `FIREBASE_SERVICE_ACCOUNT_JSON`,
`ALLOWED_EMAILS`) -- `app/lib/bootstrap.py::ensure_env_from_secrets`
copies these into the process env before any `core.*` import, which is
what lets `core/config.py` work unmodified whether it's reading `.env`
locally or Streamlit secrets in the cloud. Only these four keys are
needed -- the dashboard never calls the LLM or CoinDCX directly, so it
needs none of the worker's other secrets.

## Going live (real money -- Phase 7)

Everything below is OFF by default. Nothing here places a real order
until every step is done:

1. **Let a strategy prove itself in paper trading first.** The
   Strategies page's "Live-promotion status" section shows each
   strategy's aggregated paper track record against spec section 5's
   strict bar (20+ trades, 14+ days, profit factor >=1.2, drawdown
   <15%, beats BTC buy-and-hold).
2. **Promote it**, on Controls & Settings (auth-gated), once it clears
   the bar -- typed confirmation required. This only makes the
   strategy *eligible*; it still needs an alive live wallet to ever
   actually trade.
3. **Create a live wallet**: `python -m scripts.create_live_wallet <starting_capital_inr>`
   -- only create this with capital you're OK calling a total loss.
   Starts PAUSED.
4. **Resume it** on Controls & Settings.
5. **Turn ON live trading** on Controls & Settings -- typed
   confirmation required (`ENABLE LIVE TRADING`). This is the single
   global switch `core/coindcx/client.py::create_order`'s `allow_live`
   is threaded from, read fresh every tick.

The kill switch (same page) instantly blocks every new entry, paper
and live, but never blocks an exit. Turning live trading back OFF
behaves the same way -- open live positions still exit normally.
**Before step 5 with any real size**, verify
`core/live_engine.py`'s order-response parsing against one real tiny
order -- see Known risks below, this was NOT live-verified while
building it.

## Switching LLM providers

The LLM provider is a factory (`core/llm/provider.py`, added Phase 5)
keyed off the `settings` table's `llm.provider`/`llm.model` (see
`CLAUDE.md`'s config/settings split) -- switching from NVIDIA to
Anthropic or OpenAI is a Settings-page edit plus the matching API key
in `.env`/Render env vars, not a code change. `langchain-anthropic`/
`langchain-openai` are pinned (Phase 8) but **NOT live-verified** --
`ANTHROPIC_API_KEY`/`OPENAI_API_KEY` are empty in this repo's `.env`.
Confirm an actual successful structured-output completion for whichever
one you switch to before relying on it live; see `core/llm/provider.py
::get_llm`'s docstring for what IS confirmed (both fail cleanly with no
key, at different points in the call chain) vs. what isn't.

## Known risks

- **No on-exchange stop-loss for spot, in practice.** The API schema
  allows `stop_limit`/`take_profit_limit` order types, and docs.coindcx.com's
  example response shows them -- but a live query against **all 339**
  real INR spot pairs (2026-10-01) found **zero** that actually offer
  anything beyond `limit_order`/`market_order` (see
  `core/coindcx/client.py`'s module docstring). Spec section 10 asked
  to flag this if it turned out to be true: `core/live_engine.py`
  (Phase 7) cannot place an exchange-side stop and simulates one by
  polling, submitting a real market sell when the stop level is
  crossed -- a real gap window exists between the price crossing the
  stop and the bot's next tick actually firing the sell. Re-check
  `order_types` on the pairs you actually trade before going live;
  this could change.
- **`core/live_engine.py`'s order lifecycle (status strings, field
  names on create/status/trade_history/active_orders responses) is
  built from docs.coindcx.com, NOT a real order** -- the CoinDCX
  account had ~₹1 INR balance (dust) when this was written, not
  enough to clear any pair's minimum notional. Place one tiny real
  order manually and confirm the actual response shapes match before
  ever turning on live trading with meaningful size (Controls &
  Settings' "Turn ON live trading" toggle).
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
- **No LangGraph checkpointer is wired up.** `worker/agent/graph.py`
  compiles with none -- v1 has no human-in-the-loop interrupt to pause/
  resume across requests, and the worker is stateless by design
  anyway, so there was nothing for a checkpointer to persist. Revisit
  `langchain-cockroachdb`'s `CockroachDBSaver` only if a future phase
  adds a mid-cycle interrupt (e.g. a dashboard approval step).
- **The `strategize`/`decide` LLM nodes see only the last 100 1h
  candles and each pair's current `strategies.params` row** -- the LLM
  picks a strategy TYPE and sizes/judges a proposed entry, but never
  tunes a strategy's params; params stay at whatever
  `scripts/seed_strategies.py` seeded until a dashboard control for
  that exists.
