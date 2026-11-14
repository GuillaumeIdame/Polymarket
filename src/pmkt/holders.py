"""Holder analytics: concentration and moves between two point-in-time snapshots.

Input: api_holders snapshots (Data API v2 /holders?include_pnl=true), one row per (snapshot, wallet, token).
Caveats (verified 2026-11-14):
- /holders returns the TOP holders only (we page to 400/outcome); concentration is over the returned set
  unless `supply` (on-chain / OI) is passed.
- avg_price == 0 with amount > 0 = entry cost unknown to the API (position from split/convert/transfer,
  e.g. neg-risk minters holding tens of millions of NO). Such rows are excluded from entry statistics and
  counted in `n_entry_unknown`; they are not "free" positions.
- Data API `/v2/holders` has no protocol-contract filtering guarantee; pass `exclude` to drop them.
"""
from __future__ import annotations

import polars as pl


def concentration(h: pl.DataFrame, supply: pl.DataFrame | None = None,
                  exclude: list[str] | None = None) -> pl.DataFrame:
    """Per (condition, token): top1/5/10 share, HHI (0-10000), n holders, amount-weighted avg entry
    (known entries only)."""
    if exclude:
        h = h.filter(~pl.col("proxy_wallet").is_in([a.lower() for a in exclude]))
    known = pl.col("avg_price") > 0
    g = h.sort("amount", descending=True).group_by(["condition_id", "token_id", "outcome_index"])
    out = g.agg(
        n_holders=pl.len(),
        top_amount=pl.col("amount").sum(),
        top1=pl.col("amount").first(),
        top5=pl.col("amount").head(5).sum(),
        top10=pl.col("amount").head(10).sum(),
        hhi_raw=(pl.col("amount") ** 2).sum(),
        avg_entry_w=(pl.col("avg_price") * pl.col("amount")).filter(known).sum()
        / pl.col("amount").filter(known).sum(),
        n_entry_unknown=(~known).sum(),
        amount_entry_unknown=pl.col("amount").filter(~known).sum(),
        upnl_top=pl.col("unrealized_pnl").sum(),
        top_wallet=pl.col("proxy_wallet").first(),
        top_wallet_entry=pl.col("avg_price").first(),
    )
    if supply is not None:
        out = out.join(supply, on="token_id", how="left")
    else:
        out = out.with_columns(supply=pl.col("top_amount"))
    return out.with_columns(
        top1_share=pl.col("top1") / pl.col("supply"),
        top5_share=pl.col("top5") / pl.col("supply"),
        top10_share=pl.col("top10") / pl.col("supply"),
        hhi=10000 * pl.col("hhi_raw") / pl.col("supply") ** 2,
    ).drop("hhi_raw")


def moves(prev: pl.DataFrame, cur: pl.DataFrame, min_shares: float = 1000, min_rel: float = 0.2) -> pl.DataFrame:
    """Classify holder changes: NEW, ADD, REDUCE, EXIT (within the top-holder window), and FLIP
    (wallet held one outcome of a condition before and the other now)."""
    k = ["condition_id", "token_id", "proxy_wallet"]
    a = prev.select(*k, "outcome_index", amt_prev="amount", entry_prev="avg_price")
    b = cur.select(*k, "outcome_index", amt_cur="amount", entry_cur="avg_price",
                   upnl="unrealized_pnl", pseudonym="pseudonym")
    j = a.join(b, on=[*k, "outcome_index"], how="full", coalesce=True).with_columns(
        pl.col("amt_prev").fill_null(0), pl.col("amt_cur").fill_null(0))
    j = j.with_columns(delta=pl.col("amt_cur") - pl.col("amt_prev"))
    rel = pl.col("delta").abs() / pl.max_horizontal(pl.col("amt_prev"), pl.lit(1.0))
    j = j.with_columns(kind=pl.when((pl.col("amt_prev") == 0) & (pl.col("amt_cur") > 0)).then(pl.lit("NEW"))
                       .when((pl.col("amt_cur") == 0) & (pl.col("amt_prev") > 0)).then(pl.lit("EXIT"))
                       .when(pl.col("delta") > 0).then(pl.lit("ADD"))
                       .otherwise(pl.lit("REDUCE")))
    j = j.filter((pl.col("delta").abs() >= min_shares) & ((rel >= min_rel) | pl.col("kind").is_in(["NEW", "EXIT"])))
    # flips: same wallet+condition, EXIT on one outcome and NEW on another
    ex = j.filter(pl.col("kind") == "EXIT").select("condition_id", "proxy_wallet", from_outcome="outcome_index")
    nw = j.filter(pl.col("kind") == "NEW").select("condition_id", "proxy_wallet", to_outcome="outcome_index")
    flips = ex.join(nw, on=["condition_id", "proxy_wallet"]).filter(pl.col("from_outcome") != pl.col("to_outcome"))
    j = j.join(flips.select("condition_id", "proxy_wallet").unique().with_columns(flip=pl.lit(True)),
               on=["condition_id", "proxy_wallet"], how="left").with_columns(pl.col("flip").fill_null(False))
    return j.sort(pl.col("delta").abs(), descending=True)
