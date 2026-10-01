"""Picks which INR spot pairs paper wallets trade each tick -- the agent
finds its own coins instead of a hand-typed watchlist. Ranks CoinDCX's
public ticker (one call for every market) by 24h INR volume, after
dropping pairs too costly or too wild to survive on:

- `max_spread_pct`: bid/ask spread is paid on top of fees+TDS on every
  round trip; a wide-spread pair needs an edge it will rarely have.
- `max_abs_change_24h_pct`: a coin up/down this much in a day is a pump
  or a crash in progress -- volume ranks it high, survival says skip.
- `exclude`: stablecoins/gold-pegged tokens -- liquid, but nothing to
  trade (USDTINR tops the volume list and moves ~0.1% a day).

Ticker `volume` is already quote (INR) volume -- verified live: BTCINR
read ~4.1 crore while volume * last_price would be ~3.4e14 INR.
"""

from __future__ import annotations

DEFAULT_UNIVERSE = {
    "top_n": 8,
    "min_volume_inr": 2_000_000,
    "max_spread_pct": 0.8,
    "max_abs_change_24h_pct": 30,
    "exclude": ["USDTINR", "USDCINR", "DAIINR", "FDUSDINR", "PAXGINR", "XAUTINR"],
}


def top_inr_symbols(ticker_rows: list[dict], cfg: dict) -> list[str]:
    cfg = {**DEFAULT_UNIVERSE, **cfg}
    exclude = set(cfg["exclude"])
    ranked = []
    for row in ticker_rows:
        symbol = row.get("market", "")
        if not symbol.endswith("INR") or symbol in exclude:
            continue
        try:
            volume = float(row["volume"])
            last, bid, ask = float(row["last_price"]), float(row["bid"]), float(row["ask"])
            change = abs(float(row.get("change_24_hour") or 0))
        except (KeyError, TypeError, ValueError):
            continue  # missing/garbled quote -- can't judge cost, skip
        if last <= 0 or bid <= 0 or ask < bid:
            continue
        if volume < cfg["min_volume_inr"]:
            continue
        if (ask - bid) / last * 100 > cfg["max_spread_pct"]:
            continue
        if change > cfg["max_abs_change_24h_pct"]:
            continue
        ranked.append((volume, symbol))
    ranked.sort(reverse=True)
    return [symbol for _, symbol in ranked[: cfg["top_n"]]]
