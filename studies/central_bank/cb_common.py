"""Shared constants and helpers for the Central Bank (FOMC) market-making study.

Paper only: nothing in this folder places, signs or cancels orders, and nothing reads a private key.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "src"))

import os  # noqa: E402

from pmkt.http import Http  # noqa: E402
from pmkt.storage import connect, utc_now, write_parquet  # noqa: E402,F401

# raw Parquet lake (git-ignored); PMKT_DATA_DIR is /data inside the container
DATA_DIR = Path(os.environ.get("PMKT_DATA_DIR", ROOT / "data")) / "central_bank"
OUT_DIR = HERE / "out"                       # small, committed outputs for the dashboard
MARKETS_FILE = OUT_DIR / "markets.json"

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
DATA_API = "https://data-api.polymarket.com/v2"
WS_MARKET = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# Meetings from the study prompt. `title` is matched exactly against Gamma event titles.
MEETINGS = [
    {"meeting": "Dec-2026", "title": "Fed Decision in December?", "date": "2026-12-09",
     "zq": {"contract": "ZQZ6", "pre": "ZQX6"}},
    {"meeting": "Jan-2027", "title": "Fed Decision in January?", "date": "2027-01-27",
     "zq": {"contract": "ZQF7", "post": "ZQG7"}},
    {"meeting": "Mar-2027", "title": "Fed Decision in March?", "date": "2027-03-17",
     "zq": {"contract": "ZQH7", "pre": "ZQG7", "check": "ZQJ7"}},
]

# Expected move (bp) implied by each outcome bucket, used for the curve check.
# Assumption: "50+ bps" buckets are valued at 50 bp (the floor); flagged in reports.
OUTCOME_BP = {"50+ bps decrease": -50, "25 bps decrease": -25, "no change": 0,
              "25 bps increase": 25, "50+ bps increase": 50}

SNAPSHOT_EVERY_S = 5
LEVELS = 5


def http() -> Http:
    return Http(timeout_s=20, max_retries=5, min_interval_s=0.05)


def load_markets() -> list[dict]:
    return json.loads(MARKETS_FILE.read_text())["outcomes"]


def bp_for(label: str) -> int | None:
    return OUTCOME_BP.get(label.strip().lower())


def iso(ts: datetime | None = None) -> str:
    return (ts or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
