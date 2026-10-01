"""Replays a strategy over historical candles through the exact same
fee/slippage/TDS model the paper engine uses (core/fees.py), one
candle at a time, with no look-ahead -- `generate_signal` only ever
sees `candles[:i]`, never a future candle.

Position sizing here uses only `max_position_size_pct` of current cash
(not the full risk_manager gate stack) -- a backtest is testing
whether THIS strategy's signals are any good in isolation; confidence
and reward:cost checks are LLM/proposal concepts that don't exist yet
at this layer (Phase 5 adds the LLM). Replaying the live risk gate here
would be testing the risk manager again, not the strategy.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.fees import simulate_fill
from core.strategies.registry import generate_signal


@dataclass(frozen=True)
class BacktestTrade:
    opened_at: int  # candle `time` (ms epoch)
    closed_at: int
    entry_price: float
    exit_price: float
    qty: float
    pnl: float
    exit_reason: str  # stop_loss | take_profit | strategy_exit


@dataclass(frozen=True)
class BacktestResult:
    trades: list[BacktestTrade] = field(default_factory=list)
    net_pnl: float = 0.0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    max_drawdown_pct: float = 0.0

    @property
    def trade_count(self) -> int:
        return len(self.trades)


def run_backtest(
    candles: list[dict],
    strategy_type: str,
    raw_params: dict,
    *,
    starting_capital: float,
    costs_settings: dict,
    max_position_size_pct: float,
) -> BacktestResult:
    cash = starting_capital
    position: dict | None = None
    trades: list[BacktestTrade] = []
    equity_curve = [starting_capital]

    for i in range(1, len(candles) + 1):
        history = candles[:i]
        candle = candles[i - 1]

        if position is not None:
            exit_price, exit_reason = None, None
            if candle["low"] <= position["stop_loss"]:
                exit_price, exit_reason = position["stop_loss"], "stop_loss"
            elif candle["high"] >= position["take_profit"]:
                exit_price, exit_reason = position["take_profit"], "take_profit"
            else:
                signal = generate_signal(strategy_type, history, raw_params, has_position=True)
                if signal.action == "sell":
                    exit_price, exit_reason = candle["close"], "strategy_exit"

            if exit_price is not None:
                fill = simulate_fill(
                    "sell", position["qty"], exit_price,
                    fee_pct=costs_settings["taker_fee_pct"], slippage_pct=costs_settings["slippage_pct"],
                    tds_pct=costs_settings["tds_pct"], gst_pct=costs_settings.get("gst_pct", 0.0),
                )
                pnl = fill.cash_delta - position["cost_basis"]
                cash += fill.cash_delta
                trades.append(BacktestTrade(
                    opened_at=position["opened_at"], closed_at=candle["time"],
                    entry_price=position["entry_price"], exit_price=fill.fill_price,
                    qty=position["qty"], pnl=pnl, exit_reason=exit_reason,
                ))
                position = None

        if position is None:
            signal = generate_signal(strategy_type, history, raw_params, has_position=False)
            if signal.action == "buy" and signal.stop_loss is not None and signal.take_profit is not None:
                size_inr = cash * max_position_size_pct / 100
                entry_price = candle["close"]
                qty = size_inr / entry_price
                fill = simulate_fill(
                    "buy", qty, entry_price,
                    fee_pct=costs_settings["taker_fee_pct"], slippage_pct=costs_settings["slippage_pct"],
                    gst_pct=costs_settings.get("gst_pct", 0.0),
                )
                cost_basis = qty * fill.fill_price + fill.fee
                if -fill.cash_delta <= cash and qty > 0:
                    cash += fill.cash_delta
                    position = {
                        "qty": qty, "entry_price": fill.fill_price, "cost_basis": cost_basis,
                        "stop_loss": signal.stop_loss, "take_profit": signal.take_profit,
                        "opened_at": candle["time"],
                    }

        mark_to_market = position["qty"] * candle["close"] if position else 0.0
        equity_curve.append(cash + mark_to_market)

    return BacktestResult(
        trades=trades,
        net_pnl=sum(t.pnl for t in trades),
        win_rate=_win_rate(trades),
        profit_factor=_profit_factor(trades),
        max_drawdown_pct=_max_drawdown_pct(equity_curve),
    )


def _win_rate(trades: list[BacktestTrade]) -> float:
    if not trades:
        return 0.0
    return sum(1 for t in trades if t.pnl > 0) / len(trades)


def _profit_factor(trades: list[BacktestTrade]) -> float:
    gains = sum(t.pnl for t in trades if t.pnl > 0)
    losses = -sum(t.pnl for t in trades if t.pnl < 0)
    if losses == 0:
        return float("inf") if gains > 0 else 0.0
    return gains / losses


def _max_drawdown_pct(equity_curve: list[float]) -> float:
    peak = equity_curve[0]
    max_dd = 0.0
    for value in equity_curve:
        peak = max(peak, value)
        if peak > 0:
            max_dd = max(max_dd, (peak - value) / peak * 100)
    return max_dd
