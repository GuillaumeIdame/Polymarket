"""Gamma API: point-in-time snapshots of events and markets.

Each run writes one immutable snapshot (snapshot_ts on every row) to raw/gamma_markets.
`raw_json` keeps the full upstream object so new fields are never lost.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import Any

import polars as pl

from .http import Http
from .storage import utc_now, write_parquet

log = logging.getLogger(__name__)

# Gamma tag slug -> our category (fee table / playbook). First match wins.
CATEGORY_TAGS = [
    ("geopolitics", "geopolitics"), ("politics", "politics"), ("elections", "politics"),
    ("economy", "economics"), ("economics", "economics"), ("fed", "economics"), ("inflation", "economics"),
    ("crypto", "crypto"), ("sports", "sports"), ("esports", "sports"), ("finance", "finance"),
    ("weather", "weather"), ("tech", "tech"), ("mentions", "mentions"), ("culture", "culture"),
    ("pop-culture", "culture"),
]


def _json_list(v: Any) -> list:
    if v is None:
        return []
    if isinstance(v, list):
        return v
    try:
        out = json.loads(v)
        return out if isinstance(out, list) else []
    except (TypeError, ValueError):
        return []


def _f(v: Any) -> float | None:
    try:
        return None if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return None


def category_from_tags(tag_slugs: list[str]) -> str:
    s = set(tag_slugs)
    for slug, cat in CATEGORY_TAGS:
        if slug in s:
            return cat
    return "other"


def iter_events(http: Http, base: str, closed: bool = False, page_limit: int = 100,
                max_pages: int = 2000) -> Iterator[dict]:
    params: dict[str, Any] = {"limit": page_limit, "closed": str(closed).lower()}
    for _ in range(max_pages):
        body = http.get(f"{base}/events/keyset", params)
        yield from body.get("events") or []
        cur = body.get("next_cursor")
        if not cur:
            return
        params["after_cursor"] = cur
    log.warning("iter_events hit max_pages")


def flatten_market(ev: dict, m: dict, snapshot_ts) -> dict:
    tags = [t.get("slug") for t in (ev.get("tags") or []) if t.get("slug")]
    outcomes = _json_list(m.get("outcomes"))
    prices = [_f(p) for p in _json_list(m.get("outcomePrices"))]
    version = m.get("version") or "v1"
    # Trading identifiers: CTF tokenId for v1, PositionManager positionId for v2 (docs: onchain-position-data)
    clob_ids = [str(x) for x in _json_list(m.get("clobTokenIds"))]
    pos_ids = [str(x) for x in (m.get("positionIds") or [])]
    asset_ids = pos_ids if version == "v2" and pos_ids else clob_ids
    fs = m.get("feeSchedule") or {}
    return {
        "snapshot_ts": snapshot_ts,
        "market_id": str(m.get("id")),
        "condition_id": m.get("conditionId"),
        "question_id": m.get("questionID"),
        "event_id": str(ev.get("id")),
        "event_slug": ev.get("slug"),
        "event_title": ev.get("title"),
        "slug": m.get("slug"),
        "question": m.get("question"),
        "group_item_title": m.get("groupItemTitle"),
        "category": category_from_tags(tags),
        "tags": tags,
        "version": version,
        "neg_risk": bool(m.get("negRisk")),
        "outcomes": [str(o) for o in outcomes],
        "outcome_prices": prices,
        "asset_ids": asset_ids,
        "clob_token_ids": clob_ids,
        "position_ids": pos_ids,
        "best_bid": _f(m.get("bestBid")),
        "best_ask": _f(m.get("bestAsk")),
        "last_trade_price": _f(m.get("lastTradePrice")),
        "spread": _f(m.get("spread")),
        "volume_24h": _f(m.get("volume24hr")),
        "volume_total": _f(m.get("volumeNum") or m.get("volume")),
        "liquidity": _f(m.get("liquidityNum") or m.get("liquidity")),
        "start_date": m.get("startDate"),
        "end_date": m.get("endDate"),
        "game_start_time": m.get("gameStartTime"),
        "active": bool(m.get("active")),
        "closed": bool(m.get("closed")),
        "accepting_orders": bool(m.get("acceptingOrders")),
        "tick_size": _f(m.get("orderPriceMinTickSize")),
        "min_order_size": _f(m.get("orderMinSize")),
        "fees_enabled": bool(m.get("feesEnabled")),
        "fee_rate": _f(fs.get("rate")),
        "fee_exponent": _f(fs.get("exponent")),
        "maker_rebate_rate": _f(fs.get("rebateRate")),
        "rewards_min_size": _f(m.get("rewardsMinSize")),
        "rewards_max_spread": _f(m.get("rewardsMaxSpread")),
        "uma_resolution_status": m.get("umaResolutionStatus"),
        "resolution_source": m.get("resolutionSource") or ev.get("resolutionSource"),
        "description": m.get("description"),
        "raw_json": json.dumps(m, separators=(",", ":")),
    }


SLIM_DROP = ["raw_json", "description"]


def snapshot_markets(http: Http, base: str, data_dir, closed: bool = False, full: bool = False,
                     include_closed_markets: bool = False, chunk_events: int = 2000) -> pl.DataFrame:
    """Snapshot open events and their markets.

    slim (default, for frequent refresh): open markets only, no raw_json/description -> raw/gamma_markets.
    full (daily): also raw_json/description -> raw/gamma_markets_full.
    Written in chunks so memory stays bounded; returns the slim frame for in-process use.
    """
    ts = utc_now()
    dataset = "gamma_markets_full" if full else "gamma_markets"
    rows, slim_parts, n_ev, n_mk = [], [], 0, 0

    def flush():
        nonlocal rows, n_mk
        if not rows:
            return
        df = pl.DataFrame(rows, infer_schema_length=None)
        if not full:
            df = df.drop(SLIM_DROP)
        write_parquet(data_dir, dataset, df, ts, tag=f"c{len(slim_parts)}")
        slim_parts.append(df.drop([c for c in SLIM_DROP if c in df.columns]))
        n_mk += df.height
        rows = []

    for ev in iter_events(http, base, closed=closed):
        n_ev += 1
        for m in ev.get("markets") or []:
            if not include_closed_markets and m.get("closed"):
                continue
            rows.append(flatten_market(ev, m, ts))
        if n_ev % chunk_events == 0:
            flush()
    flush()
    out = pl.concat(slim_parts, how="diagonal_relaxed") if slim_parts else pl.DataFrame()
    if out.height:
        out = out.unique(subset=["market_id"], keep="first")
    log.info("gamma %s snapshot: %d events, %d markets", dataset, n_ev, n_mk)
    return out


def universe_candidates(http: Http, base: str, ucfg: dict) -> pl.DataFrame:
    """Targeted Gamma pull for the recorder (no full crawl): top markets by 24h volume, markets ending
    within the window, and all open markets of events carrying the playbook tags. Verified 2026-11-14:
    /markets supports order=volume24hr + offset (<10k) and end_date_min/max; /events/keyset supports tag_slug."""
    ts = utc_now()
    rows: dict[str, dict] = {}

    def add_market(ev: dict, m: dict) -> None:
        if not m.get("closed"):
            r = flatten_market(ev, m, ts)
            r.pop("raw_json"), r.pop("description")
            rows[r["market_id"]] = r

    n_top = ucfg["max_markets"]
    end_max = ts.timestamp() + ucfg["resolving_within_hours"] * 3600
    from datetime import datetime, timezone
    queries = [
        {"order": "volume24hr", "ascending": "false", "closed": "false"},
        {"order": "volume24hr", "ascending": "false", "closed": "false",
         "end_date_min": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
         "end_date_max": datetime.fromtimestamp(end_max, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")},
    ]
    for q in queries:
        for off in range(0, n_top, 100):
            ms = http.get(f"{base}/markets", {**q, "limit": 100, "offset": off})
            for m in ms:
                evs = m.get("events") or [{}]
                add_market(evs[0], m)
            if len(ms) < 100:
                break
    for slug in ucfg["include_tag_slugs"]:
        params: dict = {"limit": 100, "closed": "false", "tag_slug": slug}
        for _ in range(20):
            body = http.get(f"{base}/events/keyset", params)
            for ev in body.get("events") or []:
                for m in ev.get("markets") or []:
                    add_market(ev, m)
            if not body.get("next_cursor"):
                break
            params["after_cursor"] = body["next_cursor"]
    # /markets embeds events without tags -> batch-lookup tags and recompute category
    missing = sorted({r["event_id"] for r in rows.values() if not r["tags"] and r["event_id"] != "None"})
    tag_map: dict[str, list[str]] = {}
    for i in range(0, len(missing), 50):
        evs = http.get(f"{base}/events", [("id", e) for e in missing[i:i + 50]] + [("limit", 100)])
        for ev in evs:
            tag_map[str(ev.get("id"))] = [t.get("slug") for t in (ev.get("tags") or []) if t.get("slug")]
    for r in rows.values():
        if not r["tags"] and r["event_id"] in tag_map:
            r["tags"] = tag_map[r["event_id"]]
            r["category"] = category_from_tags(r["tags"])
    df = pl.DataFrame(list(rows.values()), infer_schema_length=None) if rows else pl.DataFrame()
    log.info("universe candidates: %d markets (%d event tag lookups)", df.height, len(missing))
    return df
