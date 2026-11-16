"""Load the recorded FOMC books/trades into Polars frames with derived columns."""
from __future__ import annotations

import polars as pl
from cb_common import DATA_DIR, LEVELS, connect, load_markets


def books() -> pl.DataFrame:
    con = connect(DATA_DIR)
    df = con.sql("SELECT * FROM cb_books ORDER BY meeting, outcome, ts").pl()
    bsz = [pl.col(f"bid_sz_{i}").fill_null(0) for i in range(1, LEVELS + 1)]
    asz = [pl.col(f"ask_sz_{i}").fill_null(0) for i in range(1, LEVELS + 1)]
    return df.with_columns(
        tob_imb=((pl.col("bid_sz_1") - pl.col("ask_sz_1")) / (pl.col("bid_sz_1") + pl.col("ask_sz_1"))),
        l5_imb=((pl.sum_horizontal(bsz) - pl.sum_horizontal(asz)) / (pl.sum_horizontal(bsz) + pl.sum_horizontal(asz))),
        micro_minus_mid=pl.col("microprice") - pl.col("mid"),
        anchor_minus_mid=pl.col("anchor") - pl.col("mid"),
        top_size=(pl.col("bid_sz_1") + pl.col("ask_sz_1")) / 2,
    )


def trades() -> pl.DataFrame:
    """Trades in YES terms: a NO-token trade at p is a YES trade at 1-p with the taker side flipped.
    Deduplicated on (tx_hash, token_id, price, size) because the socket can repeat a message after a reconnect."""
    con = connect(DATA_DIR)
    try:
        df = con.sql("SELECT * FROM cb_trades").pl()
    except Exception:  # noqa: BLE001 - no trades recorded yet
        return pl.DataFrame(schema={"ts": pl.Datetime("us", "UTC"), "meeting": pl.Utf8, "outcome": pl.Utf8,
                                    "price_yes": pl.Float64, "size": pl.Float64, "taker_yes": pl.Utf8,
                                    "token_side": pl.Utf8, "tx_hash": pl.Utf8})
    df = df.unique(subset=["tx_hash", "token_id", "price", "size"], keep="first")
    flip = pl.col("token_side") == "NO"
    return df.with_columns(
        ts=pl.coalesce("exch_ts", "recv_ts").dt.cast_time_unit("us"),
        price_yes=pl.when(flip).then(1 - pl.col("price")).otherwise(pl.col("price")),
        taker_yes=pl.when(flip).then(pl.when(pl.col("taker_side") == "BUY").then(pl.lit("SELL")).otherwise(pl.lit("BUY")))
        .otherwise(pl.col("taker_side")),
    ).sort("ts")


def markets_df() -> pl.DataFrame:
    return pl.DataFrame(load_markets())
