"""CLOB market-channel websocket recorder -> our own L2/trade archive.

Polymarket publishes no historical L2, so every day not recorded is lost. Design:
  * N connections, each <= assets_per_connection asset ids (protocol: docs api-reference/wss/market).
  * App-level heartbeat: text "PING" every 10 s (server answers "PONG").
  * Every message stored with local receive time (recv_ts) + exchange timestamp; price_change
    batches are exploded to one row per level change; raw JSON kept only for rare event types
    (new_market, market_resolved, tick_size_change) where it carries fields we do not parse.
  * Book reconstruction = last `book` snapshot + subsequent `price_change` rows (absolute size at level).
  * Reconnect with jittered backoff; each disconnect is logged to raw/ws_sessions so gaps are explicit.
    On reconnect the server re-sends `book` snapshots, which re-anchors reconstruction.
  * Universe refreshed from Gamma every `refresh_min`; diffs applied via subscribe/unsubscribe frames.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
import uuid
from datetime import datetime, timezone

import polars as pl
import websockets

from .storage import utc_now, write_parquet

log = logging.getLogger(__name__)

WS_SCHEMA = {
    "recv_ts": pl.Datetime("us", "UTC"), "conn_id": pl.Utf8, "event_type": pl.Utf8, "market": pl.Utf8,
    "asset_id": pl.Utf8, "exch_ts": pl.Datetime("ms", "UTC"), "price": pl.Float64, "size": pl.Float64,
    "side": pl.Utf8, "best_bid": pl.Float64, "best_ask": pl.Float64, "fee_rate_bps": pl.Float64,
    "tx_hash": pl.Utf8, "book_hash": pl.Utf8, "bids": pl.Utf8, "asks": pl.Utf8, "raw": pl.Utf8,
}


def _f(v):
    try:
        return None if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return None


def _ts_ms(v):
    try:
        return datetime.fromtimestamp(int(v) / 1000, timezone.utc) if v else None
    except (TypeError, ValueError):
        return None


def parse_message(msg: dict, recv_ts: datetime, conn_id: str) -> list[dict]:
    et = msg.get("event_type") or msg.get("type")
    base = {k: None for k in WS_SCHEMA}
    base.update(recv_ts=recv_ts, conn_id=conn_id, event_type=et, market=msg.get("market"),
                exch_ts=_ts_ms(msg.get("timestamp")))
    if et == "price_change":
        out = []
        for pc in msg.get("price_changes") or []:
            # raw + per-level hash dropped: fully captured by parsed columns, and they were ~85% of bytes
            out.append({**base, "asset_id": pc.get("asset_id"),
                        "price": _f(pc.get("price")), "size": _f(pc.get("size")), "side": pc.get("side"),
                        "best_bid": _f(pc.get("best_bid")), "best_ask": _f(pc.get("best_ask"))})
        return out
    row = {**base, "asset_id": msg.get("asset_id")}
    if et not in ("book", "best_bid_ask", "last_trade_price"):
        row["raw"] = json.dumps(msg, separators=(",", ":"))  # rare types: keep everything
    if et == "book":
        row.update(bids=json.dumps(msg.get("bids") or []), asks=json.dumps(msg.get("asks") or []),
                   book_hash=msg.get("hash"))
    elif et == "last_trade_price":
        row.update(price=_f(msg.get("price")), size=_f(msg.get("size")), side=msg.get("side"),
                   fee_rate_bps=_f(msg.get("fee_rate_bps")), tx_hash=msg.get("transaction_hash"))
    elif et == "best_bid_ask":
        row.update(best_bid=_f(msg.get("best_bid")), best_ask=_f(msg.get("best_ask")))
    elif et == "tick_size_change":
        row.update(price=_f(msg.get("new_tick_size")))
    return [row]


class Buffer:
    def __init__(self, data_dir, flush_every_s: int, flush_every_rows: int):
        self.data_dir, self.every_s, self.every_rows = data_dir, flush_every_s, flush_every_rows
        self.rows: list[dict] = []
        self.sessions: list[dict] = []
        self.last_flush = time.monotonic()
        self.total = 0

    def add(self, rows: list[dict]) -> None:
        self.rows.extend(rows)
        if len(self.rows) >= self.every_rows or time.monotonic() - self.last_flush > self.every_s:
            self.flush()

    def flush(self) -> None:
        if self.rows:
            df = pl.DataFrame(self.rows, schema=WS_SCHEMA).sort(["asset_id", "recv_ts"])
            write_parquet(self.data_dir, "ws_market", df, df["recv_ts"].min(), level=9)
            self.total += df.height
            self.rows = []
        if self.sessions:
            write_parquet(self.data_dir, "ws_sessions", pl.DataFrame(self.sessions, infer_schema_length=None))
            self.sessions = []
        self.last_flush = time.monotonic()


class Connection:
    def __init__(self, url: str, assets: set[str], buf: Buffer, cfg: dict):
        self.url, self.assets, self.buf, self.cfg = url, set(assets), buf, cfg
        self.id = uuid.uuid4().hex[:8]
        self.ws = None
        self.stop = asyncio.Event()
        self.msgs = 0

    async def update(self, add: set[str], remove: set[str]) -> None:
        self.assets |= add
        self.assets -= remove
        if self.ws is None:
            return
        if add:
            await self.ws.send(json.dumps({"assets_ids": sorted(add), "operation": "subscribe"}))
        if remove:
            await self.ws.send(json.dumps({"assets_ids": sorted(remove), "operation": "unsubscribe"}))

    async def _ping(self, ws) -> None:
        while True:
            await asyncio.sleep(self.cfg["ping_interval_s"])
            await ws.send("PING")

    async def run(self) -> None:
        backoff = 1.0
        while not self.stop.is_set():
            opened = utc_now()
            reason = "stopped"
            try:
                async with websockets.connect(self.url, ping_interval=None, max_size=2**24,
                                              open_timeout=20) as ws:
                    self.ws = ws
                    await ws.send(json.dumps({"assets_ids": sorted(self.assets), "type": "market",
                                              "custom_feature_enabled": self.cfg["custom_feature_enabled"]}))
                    pinger = asyncio.create_task(self._ping(ws))
                    backoff = 1.0
                    try:
                        while not self.stop.is_set():
                            try:
                                raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                            except asyncio.TimeoutError:
                                continue
                            if raw in ("PONG", "pong"):
                                continue
                            recv = utc_now()
                            try:
                                payload = json.loads(raw)
                            except ValueError:
                                log.debug("non-json frame: %s", str(raw)[:200])
                                continue
                            msgs = payload if isinstance(payload, list) else [payload]
                            rows = [r for m in msgs if isinstance(m, dict) for r in parse_message(m, recv, self.id)]
                            self.msgs += len(msgs)
                            self.buf.add(rows)
                    finally:
                        pinger.cancel()
            except Exception as e:  # noqa: BLE001
                reason = f"{type(e).__name__}: {e}"[:300]
                log.warning("conn %s dropped: %s", self.id, reason)
            finally:
                self.ws = None
                self.buf.sessions.append({"conn_id": self.id, "opened": opened, "closed": utc_now(),
                                          "n_assets": len(self.assets), "reason": reason})
            if not self.stop.is_set():
                await asyncio.sleep(backoff * (0.5 + random.random()))
                backoff = min(backoff * 2, 60)


def select_universe(markets: pl.DataFrame, ucfg: dict, now: datetime | None = None) -> pl.DataFrame:
    """Markets to record: top by 24h volume + tagged macro/crypto + anything resolving soon."""
    now = now or utc_now()
    m = markets.filter(pl.col("active") & ~pl.col("closed") & pl.col("accepting_orders")
                       & (pl.col("asset_ids").list.len() > 0))
    end = pl.col("end_date").str.to_datetime(time_zone="UTC", strict=False)
    hours_left = (end - pl.lit(now)).dt.total_seconds() / 3600
    tagged = pl.col("tags").list.eval(pl.element().is_in(ucfg["include_tag_slugs"])).list.any()
    m = m.with_columns(hours_left=hours_left, tagged=tagged)
    vol = pl.col("volume_24h").fill_null(0)
    # Priority book: tagged (macro/crypto playbook) markets get a reserved share, then volume fills the rest.
    reserved = int(ucfg["max_markets"] * ucfg.get("tagged_share", 0.4))
    tag_pick = m.filter(pl.col("tagged") & (vol >= ucfg.get("tagged_min_volume_24h", 1000))) \
        .sort("volume_24h", descending=True, nulls_last=True).head(reserved)
    rest = m.filter(
        (vol >= ucfg["min_volume_24h"])
        | ((pl.col("hours_left") > 0) & (pl.col("hours_left") <= ucfg["resolving_within_hours"])
           & (vol >= ucfg["min_volume_24h"] / 10))
    ).filter(~pl.col("market_id").is_in(tag_pick["market_id"].implode()))
    rest = rest.sort("volume_24h", descending=True, nulls_last=True).head(ucfg["max_markets"] - tag_pick.height)
    return pl.concat([tag_pick, rest], how="vertical_relaxed")


async def record(settings, universe_fn, duration_s: float | None = None, refresh_min: float = 30) -> dict:
    rcfg = settings["recorder"]
    buf = Buffer(settings.data_dir, rcfg["flush_every_s"], rcfg["flush_every_rows"])
    url = settings.endpoint("ws_market")
    per = rcfg["assets_per_connection"]
    conns: list[Connection] = []
    tasks: list[asyncio.Task] = []

    def spawn(assets: set[str]) -> None:
        c = Connection(url, assets, buf, rcfg)
        conns.append(c)
        tasks.append(asyncio.create_task(c.run()))

    async def apply(target: set[str]) -> None:
        current = set().union(*(c.assets for c in conns)) if conns else set()
        add, remove = target - current, current - target
        for c in conns:
            rm = c.assets & remove
            if rm:
                await c.update(set(), rm)
        add_l = sorted(add)
        for c in conns:
            room = per - len(c.assets)
            if room > 0 and add_l:
                chunk, add_l = set(add_l[:room]), add_l[room:]
                await c.update(chunk, set())
        for i in range(0, len(add_l), per):
            spawn(set(add_l[i:i + per]))
        log.info("universe: %d assets (+%d/-%d) over %d conns", len(target), len(add), len(remove), len(conns))

    await apply(universe_fn())
    t_end = time.monotonic() + duration_s if duration_s else None
    next_refresh = time.monotonic() + refresh_min * 60
    try:
        while t_end is None or time.monotonic() < t_end:
            await asyncio.sleep(1)
            if time.monotonic() - buf.last_flush > rcfg["flush_every_s"]:
                buf.flush()
            if time.monotonic() > next_refresh:
                try:
                    await apply(await asyncio.to_thread(universe_fn))
                except Exception as e:  # noqa: BLE001
                    log.error("universe refresh failed: %s", e)
                next_refresh = time.monotonic() + refresh_min * 60
    finally:
        for c in conns:
            c.stop.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        buf.flush()
    return {"rows": buf.total, "connections": len(conns), "messages": sum(c.msgs for c in conns)}
