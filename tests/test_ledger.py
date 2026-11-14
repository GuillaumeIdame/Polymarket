"""Ledger + book: each lifecycle path on synthetic transfers/fills/events with hand-computed answers."""
import json

import polars as pl

from pmkt import ledger
from pmkt.chain import FILL_SCHEMA, TRANSFER_SCHEMA
from pmkt.lifecycle import LIFECYCLE_SCHEMA

W, V, X = "0x" + "aa" * 20, "0x" + "bb" * 20, "0x" + "ee" * 20   # wallets, exchange
Z = ledger.ZERO
EX = "0xe111180000d2663c0091e4f400237545b87b996b"


def T(tx, blk, frm, to, tok, amt, li=0):
    return {"block_number": blk, "block_ts": None, "tx_hash": tx, "log_index": li, "contract": "c", "operator": X,
            "from_addr": frm, "to_addr": to, "batch_idx": 0, "token_id": tok, "amount": amt}


def F(tx, blk, w, side, tok, q, p, fee=0.0, li=1):
    return {"block_number": blk, "block_ts": None, "tx_hash": tx, "log_index": li, "exchange": EX, "order_hash": "h",
            "wallet": w, "counterparty": EX, "role": "taker", "side": side, "token_id": tok, "shares": q,
            "usdc": q * p, "price": p, "fee_usdc": fee, "builder": "0x0", "metadata": "0x0"}


def E(tx, blk, event, wallet, li=9, **a):
    r = {k: None for k in LIFECYCLE_SCHEMA}
    r.update(block_number=blk, tx_hash=tx, log_index=li, event=event, wallet=wallet, contract_name="x",
             is_context_only=False, args=json.dumps({"who": wallet, **a}), amount=a.get("amount"), payout=a.get("payout"))
    return r


def run(fills=(), transfers=(), events=(), seed=(), winners=None):
    led = ledger.build_ledger(pl.DataFrame(list(fills), schema=FILL_SCHEMA),
                              pl.DataFrame(list(transfers), schema=TRANSFER_SCHEMA),
                              pl.DataFrame(list(events), schema=LIFECYCLE_SCHEMA), {W, V}, [EX, X], winners)
    b = ledger.Book()
    for s in seed:
        b.seed(*s)
    b, flags = ledger.replay(led, b)
    f = b.frame()
    get = lambda w, t: f.filter((pl.col("wallet") == w) & (pl.col("token_id") == t)).row(0, named=True)  # noqa: E731
    return led, get, flags


def test_fill_buy_sell_matches_transfers():
    led, get, _ = run(
        fills=[F("t1", 1, W, "BUY", "Y", 100, 0.40, fee=1.0), F("t2", 2, W, "SELL", "Y", 50, 0.70, fee=0.5)],
        transfers=[T("t1", 1, EX, W, "Y", 100), T("t2", 2, W, EX, "Y", 50)])
    assert set(led["kind"]) == {"BUY", "SELL"}                     # no residual -> nothing unexplained
    r = get(W, "Y")
    assert abs(r["position"] - 50) < 1e-9 and abs(r["avg_entry"] - 0.41) < 1e-9
    assert abs(r["realized_pnl"] - (35 - 0.5 - 20.5)) < 1e-9


def test_split_then_sell_leg_total_pnl_invariant():
    led, get, _ = run(transfers=[T("s", 1, Z, W, "Y", 100), T("s", 1, Z, W, "N", 100, li=1),
                                 T("x", 2, W, EX, "N", 100)],
                      events=[E("s", 1, "PositionSplit", W, amount=100.0)],
                      fills=[F("x", 2, W, "SELL", "N", 100, 0.30)])
    assert sorted(led["kind"]) == ["SELL", "SPLIT", "SPLIT"]
    y, n = get(W, "Y"), get(W, "N")
    # no ref price before the split -> equal allocation: 0.50 each; selling N at 0.30 realises -20
    assert abs(n["realized_pnl"] + 20) < 1e-9 and abs(y["avg_entry"] - 0.5) < 1e-9
    # total = realised + Y basis vs 100 collateral: -20 + (Y worth 0.70 * 100 = 70 - 50) = 0 at fair value
    assert abs(n["realized_pnl"] + (0.70 * 100 - y["cost_basis"])) < 1e-9


def test_merge_and_redeem():
    # redeem burns Y 60 + N 50 and pays 60 -> the only subset summing to 60 is {Y}: Y won
    led, get, _ = run(seed=[(W, "Y", 100, 0.6), (W, "N", 90, 0.3)],
                      transfers=[T("m", 1, W, Z, "Y", 40), T("m", 1, W, Z, "N", 40, li=1),
                                 T("r", 2, W, Z, "Y", 60), T("r", 2, W, Z, "N", 50, li=1)],
                      events=[E("m", 1, "PositionsMerge", W, amount=40.0),
                              E("r", 2, "PayoutRedemption", W, payout=60.0)])
    y, n = get(W, "Y"), get(W, "N")
    # merge 40 pairs for 40 collateral (equal alloc 20/20): Y realises 40*(0.5-0.6)=-4, N 40*(0.5-0.3)=+8
    # redeem: Y wins 60 -> +60 - 36 = +24 ; N loses -> 0 - 15 = -15
    assert abs(y["realized_pnl"] - 20) < 1e-9 and abs(n["realized_pnl"] - (-7)) < 1e-9
    assert y["position"] == 0 and n["position"] == 0


def test_migration_carries_basis():
    led, get, _ = run(seed=[(W, "CTF_Y", 50, 0.42)],
                      transfers=[T("g", 1, W, X, "CTF_Y", 50), T("g", 1, Z, W, "V2_Y", 50, li=1)],
                      events=[E("g", 1, "PositionMigrated", W, amount=50.0)])
    assert set(led["kind"]) == {"MIGRATE_OUT", "MIGRATE_IN"}
    v = get(W, "V2_Y")
    assert abs(v["avg_entry"] - 0.42) < 1e-12 and get(W, "CTF_Y")["realized_pnl"] == 0


def test_transfer_between_tracked_wallets_carries_basis():
    led, get, flags = run(seed=[(W, "Y", 10, 0.3)], transfers=[T("q", 1, W, V, "Y", 4)])
    assert sorted(led["kind"]) == ["TRANSFER_IN", "TRANSFER_OUT"] and not flags
    assert abs(get(V, "Y")["avg_entry"] - 0.3) < 1e-12 and abs(get(W, "Y")["position"] - 6) < 1e-12


def test_inflow_from_untracked_is_flagged():
    led, _, flags = run(transfers=[T("u", 1, "0x" + "cc" * 20, W, "Y", 5)])
    assert led["flag"].to_list() == ["basis_unknown"] and flags == ["basis_unknown:TRANSFER_IN"]


def test_negrisk_convert_carries_net_basis():
    # 2 NO legs (indexSet 0b011, k=2) of 10 each @0.8 -> basis 16; receive YES3 x10 + (k-1)*10 = 10 collateral
    led, get, _ = run(seed=[(W, "NO1", 10, 0.8), (W, "NO2", 10, 0.8)],
                      transfers=[T("c", 1, W, X, "NO1", 10), T("c", 1, W, X, "NO2", 10, li=1),
                                 T("c", 1, X, W, "YES3", 10, li=2)],
                      events=[E("c", 1, "PositionsConverted", W, amount=10_000_000, indexSet=3)])
    assert {"CONVERT_OUT", "CONVERT_IN", "CONVERT_CASH"} <= set(led["kind"])
    assert abs(get(W, "YES3")["avg_entry"] - 0.6) < 1e-12          # (16 - 10) / 10
    assert abs(get(W, "NO1")["realized_pnl"]) < 1e-12


def test_redeem_from_collateral_receipt_and_ambiguity():
    tr = [T("r", 1, W, X, "A", 5), T("r", 1, W, X, "B", 3, li=1), T("r", 1, W, X, "C", 0.0, li=2)]
    led = ledger.build_ledger(pl.DataFrame([], schema=FILL_SCHEMA), pl.DataFrame(tr, schema=TRANSFER_SCHEMA),
                              pl.DataFrame([E("r", 1, "PayoutRedemption", X, payout=8.0)], schema=LIFECYCLE_SCHEMA),
                              {W}, [X], collateral_in={("r", W): 3.0})
    cash = dict(led.select("token_id", "cash").iter_rows())
    assert cash == {"A": 0.0, "B": 3.0}                         # B (3 shares) is the only subset paying 3
    led2 = ledger.build_ledger(pl.DataFrame([], schema=FILL_SCHEMA),
                               pl.DataFrame([T("r", 1, W, X, "A", 5), T("r", 1, W, X, "B", 5, li=1)], schema=TRANSFER_SCHEMA),
                               pl.DataFrame([E("r", 1, "PayoutRedemption", X, payout=5.0)], schema=LIFECYCLE_SCHEMA),
                               {W}, [X], collateral_in={("r", W): 5.0})
    assert set(led2["flag"]) == {"redeem_winner_ambiguous"} and abs(led2["cash"].sum() - 5.0) < 1e-12


def test_fee_mode_realized_matches_data_api_convention():
    _, get, _ = run(fills=[F("t", 1, W, "BUY", "Y", 10, 0.48, fee=0.1748)], transfers=[T("t", 1, EX, W, "Y", 10)])
    b = ledger.Book(fee_mode="realized")
    led = ledger.build_ledger(pl.DataFrame([F("t", 1, W, "BUY", "Y", 10, 0.48, fee=0.1748)], schema=FILL_SCHEMA),
                              pl.DataFrame([T("t", 1, EX, W, "Y", 10)], schema=TRANSFER_SCHEMA),
                              pl.DataFrame([], schema=LIFECYCLE_SCHEMA), {W}, [EX])
    b, _ = ledger.replay(led, b)
    r = b.frame().row(0, named=True)
    assert abs(r["avg_entry"] - 0.48) < 1e-12 and abs(r["realized_pnl"] + 0.1748) < 1e-12
    assert abs(get(W, "Y")["avg_entry"] - 0.49748) < 1e-12           # default: fee in basis
