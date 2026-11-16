"""Phase 1 recorder for the FOMC markets (paper study; read-only public endpoints).

Every 5 s: one POST /books for all outcome YES tokens -> top-5 levels per side.
Continuously: CLOB market websocket -> every trade (last_trade_price: price, size, taker side) on YES and NO tokens.
Anchor (ZQ futures) and news flag are stored as null with a status column until a source/calendar is supplied.
Writes daily-partitioned Parquet under data/central_bank/raw/{cb_books,cb_trades,cb_sessions}/dt=YYYY-MM-DD/.
Gaps (missed 5 s snapshots), reconnects and errors go to cb_sessions. Restart loop: run_recorder.sh.

Usage: python3 recorder.py [--hours H]   (0 = forever)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import time
from datetime import datetime, timezone

import httpx
import polars as pl
import websockets
from cb_common import (
    CLOB,
    DATA_DIR,
    LEVELS,
    SNAPSHOT_EVERY_S,
    WS_MARKET,
    load_markets,
    utc_now,
    write_parquet,
)

log = logging.getLogger("cb.recorder")

BOOK_COLS = (["ts", "exch_ts", "meeting", "outcome", "token_id", "mid", "microprice", "spread",
              "anchor", "anchor_status", "news", "news_status"]
             + [f"{s}_{k}_{i}" for s in ("bid", "ask") for i in range(1, LEVELS + 1) for k in ("px", "sz")])


def _f(v):
    try:
        return None if v in (None, "") else float(v)
    except (TypeError, ValueError):
        return None


def book_row(b: dict, meta: dict, ts: datetime) -> dict:
    # CLOB /books: bids ascending (best last), asks descending (best last)
    bids = [(float(x["price"]), float(x["size"])) for x in reversed(b.get("bids") or [])]
    asks = [(float(x["price"]), float(x["size"])) for x in reversed(b.get("asks") or [])]
    row = {"ts": ts, "exch_ts": datetime.fromtimestamp(int(b["timestamp"]) / 1000, timezone.utc)
           if b.get("timestamp") else None, "meeting": meta["meeting"], "outcome": meta["outcome"],
           "token_id": meta["yes_token"], "anchor": None, "anchor_status": "no_source",
           "news": None, "news_status": "no_calendar"}
    for side, lv in (("bid", bids), ("ask", asks)):
        for i in range(LEVELS):
            p, s = lv[i] if i < len(lv) else (None, None)
            row[f"{side}_px_{i + 1}"], row[f"{side}_sz_{i + 1}"] = p, s
    bb, ba, bs, as_ = row["bid_px_1"], row["ask_px_1"], row["bid_sz_1"], row["ask_sz_1"]
    if bb is not None and ba is not None:
        row["mid"] = (bb + ba) / 2
        row["spread"] = ba - bb
        row["microprice"] = (bb * as_ + ba * bs) / (bs + as_) if (bs or 0) + (as_ or 0) > 0 else None
    else:
        row["mid"] = row["spread"] = row["microprice"] = None
    return row


class Sink:
    def __init__(self, every_s: int = 60):
        self.books, self.trades, self.sessions = [], [], []
        self.every_s, self.last = every_s, time.monotonic()
        self.n_books = self.n_trades = 0

    def maybe_flush(self, force: bool = False) -> None:
        if not force and time.monotonic() - self.last < self.every_s:
            return
        if self.books:
            df = pl.DataFrame(self.books, schema_overrides={c: pl.Float64 for c in BOOK_COLS if "_px_" in c or "_sz_" in c or c in ("mid", "microprice", "spread", "anchor", "news")}, infer_schema_length=None).select(BOOK_COLS)
            write_parquet(DATA_DIR, "cb_books", df, df["ts"].min())
            self.n_books += df.height
            self.books = []
        if self.trades:
            df = pl.DataFrame(self.trades, infer_schema_length=None)
            write_parquet(DATA_DIR, "cb_trades", df, df["recv_ts"].min())
            self.n_trades += df.height
            self.trades = []
        if self.sessions:
            write_parquet(DATA_DIR, "cb_sessions", pl.DataFrame(self.sessions, infer_schema_length=None))
            self.sessions = []
        self.last = time.monotonic()

    def event(self, kind: str, detail: str = "", gap_s: float | None = None) -> None:
        self.sessions.append({"ts": utc_now(), "kind": kind, "detail": detail[:500], "gap_s": gap_s})
        log.info("%s %s", kind, detail[:200])


async def poll_books(markets: list[dict], sink: Sink, stop: asyncio.Event) -> None:
    by_token = {m["yes_token"]: m for m in markets}
    payload = [{"token_id": t} for t in by_token]
    last_ok = None
    async with httpx.AsyncClient(timeout=10, headers={"User-Agent": "pmkt-research/0.1"}) as client:
        next_t = time.monotonic()
        while not stop.is_set():
            next_t += SNAPSHOT_EVERY_S
            ts = utc_now()
            try:
                r = await client.post(f"{CLOB}/books", json=payload)
                r.raise_for_status()
                for b in r.json():
                    meta = by_token.get(b.get("asset_id"))
                    if meta:
                        sink.books.append(book_row(b, meta, ts))
                if last_ok is not None and (ts - last_ok).total_seconds() > 3 * SNAPSHOT_EVERY_S:
                    sink.event("gap_books", f"{(ts - last_ok).total_seconds():.0f}s since last snapshot",
                               (ts - last_ok).total_seconds())
                last_ok = ts
            except Exception as e:  # noqa: BLE001 - logged as a gap, never filled
                sink.event("error_books", repr(e))
            sink.maybe_flush()
            await asyncio.sleep(max(0.0, next_t - time.monotonic()))


async def stream_trades(markets: list[dict], sink: Sink, stop: asyncio.Event) -> None:
    side_of = {}
    for m in markets:
        side_of[m["yes_token"]] = (m, "YES")
        side_of[m["no_token"]] = (m, "NO")
    backoff = 1.0
    while not stop.is_set():
        opened = time.monotonic()
        try:
            async with websockets.connect(WS_MARKET, ping_interval=None, open_timeout=20, max_size=2**24) as ws:
                await ws.send(json.dumps({"assets_ids": sorted(side_of), "type": "market",
                                          "custom_feature_enabled": True}))
                sink.event("ws_connect", f"{len(side_of)} assets")
                backoff = 1.0
                last_ping = time.monotonic()
                while not stop.is_set():
                    if time.monotonic() - last_ping > 10:
                        await ws.send("PING")
                        last_ping = time.monotonic()
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    except asyncio.TimeoutError:
                        continue
                    if raw in ("PONG", "pong"):
                        continue
                    try:
                        payload = json.loads(raw)
                    except ValueError:
                        continue
                    for msg in payload if isinstance(payload, list) else [payload]:
                        if not isinstance(msg, dict) or msg.get("event_type") != "last_trade_price":
                            continue
                        meta, tok_side = side_of.get(msg.get("asset_id"), (None, None))
                        if not meta:
                            continue
                        sink.trades.append({
                            "recv_ts": utc_now(),
                            "exch_ts": datetime.fromtimestamp(int(msg["timestamp"]) / 1000, timezone.utc)
                            if msg.get("timestamp") else None,
                            "meeting": meta["meeting"], "outcome": meta["outcome"], "token_id": msg["asset_id"],
                            "token_side": tok_side, "price": _f(msg.get("price")), "size": _f(msg.get("size")),
                            "taker_side": msg.get("side"), "fee_rate_bps": _f(msg.get("fee_rate_bps")),
                            "tx_hash": msg.get("transaction_hash")})
                    sink.maybe_flush()
        except Exception as e:  # noqa: BLE001
            sink.event("ws_disconnect", repr(e), time.monotonic() - opened)
        if not stop.is_set():
            await asyncio.sleep(backoff * (0.5 + random.random()))
            backoff = min(60.0, backoff * 2)


async def main_async(hours: float) -> None:
    markets = load_markets()
    sink = Sink()
    stop = asyncio.Event()
    sink.event("start", f"{len(markets)} outcomes, snapshot every {SNAPSHOT_EVERY_S}s")
    tasks = [asyncio.create_task(poll_books(markets, sink, stop)),
             asyncio.create_task(stream_trades(markets, sink, stop))]
    try:
        if hours > 0:
            await asyncio.sleep(hours * 3600)
        else:
            await asyncio.Event().wait()
    finally:
        stop.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        sink.event("stop", f"books={sink.n_books + len(sink.books)} trades={sink.n_trades + len(sink.trades)}")
        sink.maybe_flush(force=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=0)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(main_async(a.hours))


if __name__ == "__main__":
    main()
