"""L2 book reconstruction from the recorded ws_market archive.

State(asset, t) = last `book` snapshot at or before t, then apply every `price_change` after it up to t.
price_change.size is the NEW absolute size at that price level (0 = level removed); side BUY = bid.
Validated against REST GET /book (see tests/live check in docs/PHASE1_STATUS.md).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime

import polars as pl


@dataclass
class Book:
    bids: dict[float, float] = field(default_factory=dict)
    asks: dict[float, float] = field(default_factory=dict)
    ts: datetime | None = None

    def apply(self, side: str, price: float, size: float) -> None:
        lvl = self.bids if side == "BUY" else self.asks
        if size <= 0:
            lvl.pop(price, None)
        else:
            lvl[price] = size

    @property
    def best_bid(self) -> float | None:
        return max(self.bids) if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return min(self.asks) if self.asks else None

    @property
    def mid(self) -> float | None:
        bb, ba = self.best_bid, self.best_ask
        return (bb + ba) / 2 if bb is not None and ba is not None else None

    def depth(self, side: str, within: float) -> float:
        """Shares within `within` of the touch on one side."""
        if side == "BUY" and self.bids:
            bb = self.best_bid
            return sum(s for p, s in self.bids.items() if p >= bb - within - 1e-12)
        if side == "SELL" and self.asks:
            ba = self.best_ask
            return sum(s for p, s in self.asks.items() if p <= ba + within + 1e-12)
        return 0.0


def reconstruct(events: pl.DataFrame, asset_id: str, as_of: datetime | None = None) -> Book:
    """events: ws_market rows (any superset). Uses recv_ts ordering (our clock = what we could act on)."""
    e = events.filter(pl.col("asset_id") == asset_id)
    if as_of is not None:
        e = e.filter(pl.col("recv_ts") <= as_of)
    e = e.filter(pl.col("event_type").is_in(["book", "price_change"])).sort("recv_ts", maintain_order=True)
    snaps = e.with_row_index("i").filter(pl.col("event_type") == "book")
    if snaps.height == 0:
        return Book()
    last = snaps.row(-1, named=True)
    b = Book(ts=last["recv_ts"])
    for lv in json.loads(last["bids"] or "[]"):
        b.apply("BUY", float(lv["price"]), float(lv["size"]))
    for lv in json.loads(last["asks"] or "[]"):
        b.apply("SELL", float(lv["price"]), float(lv["size"]))
    tail = e.slice(last["i"] + 1).filter(pl.col("event_type") == "price_change")
    for side, price, size, ts in tail.select("side", "price", "size", "recv_ts").iter_rows():
        b.apply(side, price, size)
        b.ts = ts
    return b
