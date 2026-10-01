from core.universe import top_inr_symbols


def _row(market, volume, bid=99.9, ask=100.0, last=100.0, change=1.0):
    return {"market": market, "volume": str(volume), "bid": str(bid), "ask": str(ask),
            "last_price": str(last), "change_24_hour": str(change)}


def test_ranks_by_inr_volume_and_caps_at_top_n():
    rows = [_row("BTCINR", 40_000_000), _row("ETHINR", 20_000_000), _row("SOLINR", 25_000_000)]
    assert top_inr_symbols(rows, {"top_n": 2}) == ["BTCINR", "SOLINR"]


def test_drops_non_inr_stablecoins_thin_wide_and_pumping_pairs():
    rows = [
        _row("BTCUSDT", 90_000_000),                 # not INR
        _row("USDTINR", 90_000_000),                 # excluded stablecoin
        _row("THININR", 100_000),                    # below min volume
        _row("WIDEINR", 30_000_000, bid=98.0),       # 2% spread
        _row("PUMPINR", 40_000_000, change=84.7),    # 24h pump
        _row("BADINR", "nan-ish"),                   # garbled quote
        _row("ETHINR", 20_000_000),
    ]
    assert top_inr_symbols(rows, {}) == ["ETHINR"]
