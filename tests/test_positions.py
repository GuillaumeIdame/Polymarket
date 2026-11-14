from datetime import datetime, timezone

import polars as pl

from pmkt import positions


def _fills(rows):
    base = datetime(2026, 11, 14, tzinfo=timezone.utc)
    return pl.DataFrame([{"tx_hash": f"0x{i}", "log_index": 0, "block_number": i, "block_ts": base.replace(minute=i),
                          "wallet": "w", "token_id": "t", "side": s, "shares": q, "usdc": q * p, "fee_usdc": f,
                          "role": "taker"} for i, (s, q, p, f) in enumerate(rows)])


def test_average_cost_and_realized():
    b = positions.position_book(_fills([("BUY", 100, 0.40, 1.0), ("BUY", 100, 0.60, 0.0), ("SELL", 50, 0.70, 0.5)]))
    r = b.row(0, named=True)
    # avg = (40+1+60)/200 = 0.505 ; realized = 50*(0.70-0.505) - 0.5 = 9.25
    assert abs(r["position"] - 150) < 1e-9
    assert abs(r["avg_entry"] - 0.505) < 1e-9
    assert abs(r["realized_pnl"] - 9.25) < 1e-9


def test_point_in_time():
    f = _fills([("BUY", 100, 0.40, 0.0), ("SELL", 100, 0.90, 0.0)])
    asof = f["block_ts"][0]
    assert positions.position_book(f, as_of=asof).row(0, named=True)["position"] == 100


def test_oversell_flagged_not_negative():
    r = positions.position_book(_fills([("SELL", 30, 0.5, 0.0)])).row(0, named=True)
    assert r["position"] == 0 and r["unexplained_sell_shares"] == 30 and r["realized_pnl"] == 0
