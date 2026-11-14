"""Immutable raw layer: append-only Parquet, hive-partitioned by dataset/date.

Layout: {data_dir}/raw/{dataset}/dt=YYYY-MM-DD/{part}.parquet
Every file is written once (unique part name) and never modified, so any metric can be
recomputed as-of a past timestamp from raw files alone.
DuckDB reads the whole lake via `read_parquet(... hive_partitioning=1, union_by_name=1)`.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb
import polars as pl


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def write_parquet(data_dir: Path, dataset: str, df: pl.DataFrame, ts: datetime | None = None,
                  tag: str = "", level: int = 3) -> Path | None:
    if df.height == 0:
        return None
    ts = ts or utc_now()
    d = data_dir / "raw" / dataset / f"dt={ts:%Y-%m-%d}"
    d.mkdir(parents=True, exist_ok=True)
    name = f"{ts:%H%M%S}_{tag + '_' if tag else ''}{uuid.uuid4().hex[:8]}.parquet"
    tmp = d / (name + ".tmp")
    df.write_parquet(tmp, compression="zstd", compression_level=level)
    os.replace(tmp, d / name)  # atomic: readers never see partial files
    return d / name


def glob(data_dir: Path, dataset: str) -> str:
    return str(data_dir / "raw" / dataset / "*" / "*.parquet")


def connect(data_dir: Path) -> duckdb.DuckDBPyConnection:
    """DuckDB connection with one view per raw dataset present on disk."""
    con = duckdb.connect()
    raw = data_dir / "raw"
    if raw.exists():
        for ds in sorted(p.name for p in raw.iterdir() if p.is_dir()):
            if any((raw / ds).glob("*/*.parquet")):
                con.execute(
                    f"CREATE VIEW {ds} AS SELECT * FROM read_parquet('{glob(data_dir, ds)}', "
                    f"hive_partitioning=1, union_by_name=1)"
                )
    return con


class Cursor:
    """Tiny JSON state file for resumable jobs (e.g. last processed block)."""

    def __init__(self, data_dir: Path, name: str):
        self.path = data_dir / "state" / f"{name}.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def get(self, default: Any = None) -> Any:
        if not self.path.exists():
            return default
        return json.loads(self.path.read_text())

    def set(self, value: dict) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({**value, "updated_unix": time.time()}))
        os.replace(tmp, self.path)


def query(con: duckdb.DuckDBPyConnection, sql: str) -> pl.DataFrame:
    """Run SQL and return Polars (works across DuckDB 1.x/2.x result APIs)."""
    return con.sql(sql).pl()
