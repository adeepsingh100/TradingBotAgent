"""One-time bootstrap for a LIVE (real-money) wallet -- matches
scripts/seed_wallets.py's precedent for one-time setup actions, not a
dashboard form: this is a rare, deliberate, redeploy-adjacent action
that needs real CoinDCX credentials already configured, same category
config.py's module docstring already draws a line around for script-
only actions.

Creates the agent PAUSED (not alive) -- one more deliberate click
(Resume, on Controls & Settings) between "wallet exists" and "wallet
actively ticks." Turning on live order placement ALSO still needs the
separate "Turn ON live trading" typed-confirmation toggle on that same
page -- creating this wallet alone places no real orders.

Run manually: python -m scripts.create_live_wallet <starting_capital>
"""

from __future__ import annotations

import sys

from core.coindcx import client
from core.db.models import Agent, Wallet
from core.db.session import get_session

WALLET_NAME = "live"


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: python -m scripts.create_live_wallet <starting_capital_inr>")
        sys.exit(1)
    try:
        starting_capital = float(sys.argv[1])
    except ValueError:
        print(f"'{sys.argv[1]}' isn't a number")
        sys.exit(1)
    if starting_capital <= 0:
        print("starting_capital must be positive")
        sys.exit(1)

    try:
        balances = client.get_balances()
        inr = next((b for b in balances if b.get("currency") == "INR"), None)
        real_inr = float(inr["balance"]) if inr else 0.0
        print(f"Real CoinDCX INR balance right now: ₹{real_inr:.2f}")
    except Exception as exc:  # noqa: BLE001 -- a failed balance check shouldn't block seeing the rest of this prompt
        print(f"(couldn't read real balance: {exc})")

    print(f"\nAbout to create a LIVE (real-money) wallet named '{WALLET_NAME}' with "
          f"starting_capital={starting_capital:.2f} INR.")
    print("This is bookkeeping only -- no order is placed by this script. The agent starts PAUSED, and no real "
          "order will ever be placed until you ALSO turn on live trading from Controls & Settings.")
    print("Only create this with capital you are OK calling a total loss.\n")

    confirmation = input("Type 'yes' to create this wallet: ").strip()
    if confirmation != "yes":
        print("Aborted -- nothing created.")
        sys.exit(0)

    with get_session() as session:
        existing = session.query(Wallet).filter_by(name=WALLET_NAME).one_or_none()
        if existing is not None:
            print(f"wallet '{WALLET_NAME}' already exists, aborting -- delete it manually first if you really want a fresh one")
            sys.exit(1)

        wallet = Wallet(name=WALLET_NAME, kind="live", starting_capital=starting_capital, current_cash=starting_capital)
        session.add(wallet)
        session.flush()
        session.add(Agent(wallet_id=wallet.id, status="paused", mode="live"))
        print(f"Created live wallet '{WALLET_NAME}' (id={wallet.id}), agent status=paused.")
        print("Resume it and turn on live trading from Controls & Settings when you're ready.")


if __name__ == "__main__":
    main()
