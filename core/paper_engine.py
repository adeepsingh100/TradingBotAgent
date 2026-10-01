"""Multi-wallet paper trading engine -- uses live CoinDCX prices (via
core/coindcx/client.py) but every fill is simulated through
core/fees.py, never a real order. Every function here takes an open
SQLAlchemy `session` rather than opening its own -- the caller (a
worker tick, Phase 5) runs a whole cycle in one transaction so a crash
mid-cycle can't leave cash deducted with no position recorded.

Always assumes a taker-rate fill (core/fees.py's choice, repeated here:
a paper fill is immediate against the live price, never a resting
maker order).
"""

from __future__ import annotations

from datetime import datetime, timezone

from core.db.models import Position, Trade, Wallet
from core.fees import simulate_fill


def open_position(
    session,
    wallet: Wallet,
    *,
    pair: str,
    qty: float,
    entry_price: float,
    stop_loss: float,
    take_profit: float,
    costs_settings: dict,
    strategy_id=None,
    decision_id=None,
) -> Position:
    fill = simulate_fill(
        "buy", qty, entry_price, fee_pct=costs_settings["taker_fee_pct"], slippage_pct=costs_settings["slippage_pct"],
        gst_pct=costs_settings.get("gst_pct", 0.0),
    )
    cost = -fill.cash_delta
    if cost > wallet.current_cash:
        raise ValueError(f"insufficient cash: need {cost:.2f}, have {wallet.current_cash:.2f}")

    wallet.current_cash += fill.cash_delta  # cash_delta is negative on a buy

    position = Position(
        wallet_id=wallet.id,
        pair=pair,
        side="buy",
        qty=qty,
        entry_price=fill.fill_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
        strategy_id=strategy_id,
        decision_id=decision_id,
    )
    session.add(position)
    session.flush()  # need position.id for the trade row below

    session.add(
        Trade(
            wallet_id=wallet.id,
            position_id=position.id,
            pair=pair,
            side="buy",
            qty=qty,
            price=fill.fill_price,
            fee=fill.fee,
            tds=0.0,
            slippage=fill.slippage_amount,
            pnl=None,
            strategy_id=strategy_id,
            decision_id=decision_id,
        )
    )
    return position


def close_position(
    session,
    wallet: Wallet,
    position: Position,
    *,
    exit_price: float,
    costs_settings: dict,
    decision_id=None,
) -> Trade:
    fill = simulate_fill(
        "sell",
        position.qty,
        exit_price,
        fee_pct=costs_settings["taker_fee_pct"],
        slippage_pct=costs_settings["slippage_pct"],
        tds_pct=costs_settings["tds_pct"],
        gst_pct=costs_settings.get("gst_pct", 0.0),
    )

    entry_trade = (
        session.query(Trade).filter_by(position_id=position.id, side="buy").order_by(Trade.executed_at).first()
    )
    cost_basis = entry_trade.qty * entry_trade.price + entry_trade.fee
    pnl = fill.cash_delta - cost_basis

    wallet.current_cash += fill.cash_delta
    wallet.tds_credit += fill.tds

    exit_trade = Trade(
        wallet_id=wallet.id,
        position_id=position.id,
        pair=position.pair,
        side="sell",
        qty=position.qty,
        price=fill.fill_price,
        fee=fill.fee,
        tds=fill.tds,
        slippage=fill.slippage_amount,
        pnl=pnl,
        strategy_id=position.strategy_id,
        decision_id=decision_id,
    )
    session.add(exit_trade)
    position.closed_at = datetime.now(timezone.utc)
    return exit_trade


def get_open_positions(session, wallet_id) -> list[Position]:
    return session.query(Position).filter_by(wallet_id=wallet_id, closed_at=None).all()


def check_stop_or_target(position: Position, candle_high: float, candle_low: float) -> tuple[str | None, float | None]:
    """Checked against the candle's high/low since the last tick, not
    just the latest price (spec section 6) -- a gap between ticks that
    crosses the stop or target must still count as triggered. Stop
    checked first when a single wide candle could have hit both:
    capital preservation over the best-case assumption."""
    if candle_low <= position.stop_loss:
        return "stop_loss", position.stop_loss
    if candle_high >= position.take_profit:
        return "take_profit", position.take_profit
    return None, None


def equity(wallet: Wallet, open_positions: list[Position], current_prices: dict[str, float]) -> float:
    """Cash + mark-to-market holdings -- deliberately excludes
    tds_credit (spec section 8: "TDS credits shown separately"), so
    this is exactly the number the death threshold checks."""
    holdings_value = sum(p.qty * current_prices[p.pair] for p in open_positions if p.pair in current_prices)
    return wallet.current_cash + holdings_value


def is_dead(wallet: Wallet, current_equity: float, death_threshold_pct: float) -> bool:
    return current_equity < wallet.starting_capital * death_threshold_pct / 100


def kill_wallet(session, wallet: Wallet, agent, open_positions: list[Position], current_prices: dict[str, float], costs_settings: dict) -> None:
    """Closes every open position at the last known price, then marks
    the agent permanently dead. Revival is only through the dashboard's
    typed-confirmation reset (Phase 6), never automatic."""
    for position in open_positions:
        price = current_prices.get(position.pair, position.entry_price)
        close_position(session, wallet, position, exit_price=price, costs_settings=costs_settings)

    agent.status = "dead"
    agent.died_at = datetime.now(timezone.utc)
