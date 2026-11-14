"""Data API v2 snapshots: holders (with entry/PnL economics), positions, trades.

v2 contract (docs migrate/data-api-v1-to-v2, verified 2026-11-14): payload in `data`, cursor pagination via
`pagination.next_cursor`, snake_case fields, `condition` takes <= 20 comma-separated condition ids,
v1 `market`/`offset` params are rejected with HTTP 400.
Holders are returned per token (outcome); `include_pnl=true` adds avg_price, entry_cost, PnL.
Undocumented server cap (2026-11-14): limit <= 100 when include_pnl=true (spec says 1000).
These are vendor-computed and must be reconciled vs our on-chain ledger (positions.reconcile).
"""
from __future__ import annotations

import logging

import polars as pl

from .http import Http
from .storage import utc_now, write_parquet

log = logging.getLogger(__name__)

HOLDER_COLS = ["proxy_wallet", "token_id", "outcome_index", "amount", "avg_price", "entry_cost_usdc",
               "current_price", "current_value", "realized_pnl", "unrealized_pnl", "total_pnl", "pseudonym",
               "name", "verified"]


def snapshot_holders(http: Http, base: str, data_dir, condition_ids: list[str], limit: int = 100,
                     max_pages: int = 4) -> pl.DataFrame:
    ts = utc_now()
    rows = []
    for cid in condition_ids:
        params = {"condition": cid, "limit": limit, "include_pnl": "true"}
        cur = None
        for _ in range(max_pages):
            if cur:
                params["cursor"] = cur
            body = http.get(f"{base}/holders", params)
            for tok in body.get("data") or []:
                for h in tok.get("holders") or []:
                    rows.append({"snapshot_ts": ts, "condition_id": cid,
                                 **{k: h.get(k) for k in HOLDER_COLS}})
            cur = (body.get("pagination") or {}).get("next_cursor")
            if not cur:
                break
    df = pl.DataFrame(rows, infer_schema_length=None) if rows else pl.DataFrame()
    if df.height:
        df = df.with_columns(pl.col("proxy_wallet").str.to_lowercase())
    write_parquet(data_dir, "api_holders", df, ts)
    log.info("holders snapshot: %d conditions, %d rows", len(condition_ids), df.height)
    return df


def snapshot_positions(http: Http, base: str, data_dir, user: str, status: str | None = None) -> pl.DataFrame:
    ts = utc_now()
    params = {"user": user, "limit": 500}
    if status:
        params["status"] = status
    rows = [{"snapshot_ts": ts, **r} for r in http.paginate_v2(f"{base}/positions", params)]
    df = pl.DataFrame(rows, infer_schema_length=None) if rows else pl.DataFrame()
    write_parquet(data_dir, "api_positions", df, ts)
    return df


def fetch_trades(http: Http, base: str, data_dir, condition_ids: list[str] | None = None,
                 user: str | None = None, max_pages: int = 20) -> pl.DataFrame:
    ts = utc_now()
    params: dict = {"limit": 500}
    if condition_ids:
        params["condition"] = ",".join(condition_ids[:20])
    if user:
        params["user"] = user
    rows = [{"snapshot_ts": ts, **r} for r in http.paginate_v2(f"{base}/trades", params, max_pages)]
    df = pl.DataFrame(rows, infer_schema_length=None) if rows else pl.DataFrame()
    write_parquet(data_dir, "api_trades", df, ts)
    return df
