"""Wallet position book from on-chain fills ONLY (fast approximation).

Superseded for anything that needs correct balances by `pmkt.ledger` (fills + ERC-1155 transfers +
split/merge/redeem/convert/migration, with basis carry). Kept for quick trade-level analytics.

Accounting: average-cost per (wallet, token).
  BUY  q @ p, fee f : pos += q ; cost += q*p + f
  SELL q @ p, fee f : realized += q*(p - avg) - f ; cost -= q*avg ; pos -= q
Point-in-time: pass as_of to use only fills with block_ts <= as_of.

Known Phase-1 limitation: fills alone miss split/merge/redeem/convert and wallet-to-wallet transfers.
Those live in chain_transfers; `reconcile` compares fill-derived positions with the transfer-derived
ERC-1155 balance and with Data API v2 so discrepancies are visible instead of silent.
Sells that exceed the fill-derived position (shares acquired via split/transfer) are booked at avg=price
(zero realized PnL) and flagged in `unexplained_sell_shares`.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime

import polars as pl


def position_book(fills: pl.DataFrame, as_of: datetime | None = None) -> pl.DataFrame:
    f = fills
    if as_of is not None:
        f = f.filter(pl.col("block_ts") <= as_of)
    f = f.unique(subset=["tx_hash", "log_index"]).sort(["block_number", "log_index"])
    st: dict[tuple, dict] = defaultdict(lambda: {
        "pos": 0.0, "cost": 0.0, "realized": 0.0, "fees": 0.0, "buy_sh": 0.0, "buy_usdc": 0.0,
        "sell_sh": 0.0, "sell_usdc": 0.0, "n": 0, "n_maker": 0, "first": None, "last": None, "unexpl": 0.0})
    for w, tok, side, q, usdc, fee, role, ts in f.select(
            ["wallet", "token_id", "side", "shares", "usdc", "fee_usdc", "role", "block_ts"]).iter_rows():
        s = st[(w, tok)]
        s["n"] += 1
        s["n_maker"] += role == "maker"
        s["fees"] += fee
        s["first"] = s["first"] or ts
        s["last"] = ts
        if side == "BUY":
            s["pos"] += q
            s["cost"] += usdc + fee
            s["buy_sh"] += q
            s["buy_usdc"] += usdc
        else:
            p = usdc / q if q else 0.0
            covered = min(q, max(s["pos"], 0.0))
            avg = s["cost"] / s["pos"] if s["pos"] > 1e-9 else p
            s["realized"] += covered * (p - avg) - fee
            s["cost"] -= covered * avg
            s["pos"] -= covered
            s["unexpl"] += q - covered
            s["sell_sh"] += q
            s["sell_usdc"] += usdc
            if s["pos"] < 1e-9:
                s["pos"], s["cost"] = 0.0, 0.0
    rows = []
    for (w, tok), s in st.items():
        rows.append({
            "wallet": w, "token_id": tok, "position": s["pos"], "cost_basis": s["cost"],
            "avg_entry": s["cost"] / s["pos"] if s["pos"] > 1e-9 else None,
            "realized_pnl": s["realized"], "fees_paid": s["fees"],
            "vwap_buy": s["buy_usdc"] / s["buy_sh"] if s["buy_sh"] else None,
            "vwap_sell": s["sell_usdc"] / s["sell_sh"] if s["sell_sh"] else None,
            "shares_bought": s["buy_sh"], "shares_sold": s["sell_sh"], "n_fills": s["n"],
            "maker_share": s["n_maker"] / s["n"], "first_fill": s["first"], "last_fill": s["last"],
            "unexplained_sell_shares": s["unexpl"],
        })
    return pl.DataFrame(rows, infer_schema_length=None) if rows else pl.DataFrame()


def mark(book: pl.DataFrame, marks: pl.DataFrame) -> pl.DataFrame:
    """marks: token_id, mark (mid or last). Adds market value and unrealized PnL."""
    return (book.join(marks, on="token_id", how="left")
            .with_columns(market_value=pl.col("position") * pl.col("mark"),
                          unrealized_pnl=pl.col("position") * pl.col("mark") - pl.col("cost_basis"))
            .with_columns(total_pnl=pl.col("realized_pnl") + pl.col("unrealized_pnl").fill_null(0)))


def transfer_balances(transfers: pl.DataFrame, exclude: list[str] | None = None,
                      as_of: datetime | None = None) -> pl.DataFrame:
    """ERC-1155 balance deltas over the indexed window (not absolute unless indexed from deployment)."""
    t = transfers if as_of is None else transfers.filter(pl.col("block_ts") <= as_of)
    t = t.unique(subset=["tx_hash", "log_index", "batch_idx"])
    zero = "0x" + "0" * 40
    out = pl.concat([
        t.filter(pl.col("to_addr") != zero).select(wallet="to_addr", token_id="token_id", delta="amount"),
        t.filter(pl.col("from_addr") != zero).select(wallet="from_addr", token_id="token_id",
                                                     delta=-pl.col("amount")),
    ])
    if exclude:
        out = out.filter(~pl.col("wallet").is_in([a.lower() for a in exclude]))
    return out.group_by(["wallet", "token_id"]).agg(balance_delta=pl.col("delta").sum())


def reconcile(book: pl.DataFrame, api_holders: pl.DataFrame, tol: float = 0.01) -> pl.DataFrame:
    """Compare fill-derived positions vs Data API v2 holders for overlapping (wallet, token)."""
    a = api_holders.select(wallet="proxy_wallet", token_id="token_id", api_amount="amount",
                           api_avg="avg_price")
    j = a.join(book.select("wallet", "token_id", "position", "avg_entry"), on=["wallet", "token_id"], how="left")
    return j.with_columns(
        diff=(pl.col("api_amount") - pl.col("position").fill_null(0)),
    ).with_columns(match=(pl.col("diff").abs() <= tol * pl.col("api_amount").abs().clip(lower_bound=1)))
