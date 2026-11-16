"""Shared pieces of the holder-quality scan: HTTP pool, categories, wallet scoring (PROMPT.md §2, §5)."""
from __future__ import annotations

import json
import math
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pmkt.http import Http  # noqa: E402

GAMMA = "https://gamma-api.polymarket.com"
DATA = "https://data-api.polymarket.com/v2"
OUT = HERE / "out"
ART = HERE / "art"
MAX_PARALLEL = 8

# ---------------------------------------------------------------- HTTP (one client per worker thread)
_tls = threading.local()
FAILURES: list[dict] = []
_flock = threading.Lock()


def h() -> Http:
    if not hasattr(_tls, "http"):
        _tls.http = Http(timeout_s=30, max_retries=5, min_interval_s=0.05)
    return _tls.http


def get(url: str, params: dict | None = None, what: str = ""):
    """GET with retries (429/5xx inside Http). Failures are recorded and return None, never filled."""
    try:
        return h().get(url, params)
    except Exception as e:  # noqa: BLE001
        with _flock:
            FAILURES.append({"what": what or url.rsplit("/", 1)[-1], "url": url, "params": json.dumps(params or {}),
                             "error": repr(e)[:300], "at": int(time.time())})
        return None


def pmap(fn, items, workers: int = MAX_PARALLEL):
    with ThreadPoolExecutor(workers) as ex:
        return list(ex.map(fn, items))


# ---------------------------------------------------------------- categories
CATEGORIES = [
    ("Sports", {"sports", "games", "esports", "soccer", "ufc", "nfl", "cfb", "nfl/cfb"}),
    ("Crypto", {"crypto", "crypto prices", "bitcoin", "ethereum", "solana"}),
    ("Politics", {"elections", "global elections", "main election", "politics", "trump", "congress", "courts"}),
    ("Geopolitics", {"geopolitics", "iran", "middle east", "ukraine", "russia", "military", "israel",
                     "peace deal", "hormuz"}),
    ("Macro", {"economy", "macro indicators", "fed", "fed rates", "fed/fed rates", "inflation", "global rates",
               "economic policy", "oil", "stocks", "finance"}),
]
HORIZON_DAYS = {"Sports": 4, "Crypto": 21, "Politics": 45, "Geopolitics": 90, "Macro": 90}


def categorize(tag_labels: list[str]) -> str | None:
    tags = {t.strip().lower() for t in tag_labels if t}
    if any("election" in t for t in tags):
        return "Politics"
    for name, keys in CATEGORIES:
        if tags & keys:
            return name
    return None


_KW = [
    ("Crypto", ("bitcoin", "btc", "ethereum", " eth ", "solana", "xrp", "up or down", "dogecoin", "crypto")),
    ("Macro", ("fed ", "fed?", "interest rate", "cpi", "inflation", "gdp", "unemployment", "jobs report",
               "s&p", "nasdaq", "oil", "recession", "treasury", "ecb", "boe")),
    ("Politics", ("election", "president", "senate", "governor", "mayor", "nominee", "trump", "vote", "party",
                  "parliament", "prime minister", "congress", "supreme court")),
    ("Geopolitics", ("iran", "israel", "ukraine", "russia", "ceasefire", "strike", "china", "military",
                     "nato", "gaza", "hamas", "war", "invade", "hormuz")),
    ("Sports", (" vs", "vs.", "nfl", "nba", "nhl", "mlb", "ufc", " fc", "spread", "o/u", "map ", "game ",
                "match", "win the", "champions", "league", "open", "grand prix", "cs2", "lol:", "dota")),
]
_SLUG = {"nfl": "Sports", "nba": "Sports", "nhl": "Sports", "mlb": "Sports", "ufc": "Sports", "cs2": "Sports",
         "lol": "Sports", "dota2": "Sports", "epl": "Sports", "cbb": "Sports", "cfb": "Sports", "atp": "Sports",
         "wta": "Sports", "btc": "Crypto", "eth": "Crypto", "sol": "Crypto", "xrp": "Crypto",
         "bitcoin": "Crypto", "ethereum": "Crypto", "solana": "Crypto"}


def title_category(title: str, slug: str = "") -> str:
    """Keyword heuristic used for wallet specialization (PROMPT guardrail: it is only a heuristic)."""
    head = (slug or "").split("-")[0].lower()
    if head in _SLUG:
        return _SLUG[head]
    t = f" {(title or '').lower()} "
    for name, kws in _KW:
        if any(k in t for k in kws):
            return name
    return "Other"


# ---------------------------------------------------------------- wallet record features (SMART, §5.4)
def record_features(points: list[dict], stats: dict | None, vol_fallback: dict | None, asof: float) -> dict | None:
    """Features from the cumulative daily PnL series as of `asof` (unix). Point-in-time when asof < now."""
    pts = sorted((p for p in points if p["timestamp"] <= asof), key=lambda p: p["timestamp"])
    if not pts:
        return None
    cum = [p.get("economic_pnl") or 0.0 for p in pts]
    fills = [p.get("trade_count") or 0 for p in pts]
    vol = [p.get("volume_usdc") or 0.0 for p in pts]
    d = [cum[0]] + [cum[i] - cum[i - 1] for i in range(1, len(cum))]
    active = (1 if fills[0] > 0 else 0) + sum(1 for i in range(1, len(fills)) if fills[i] > fills[i - 1])
    join = (stats or {}).get("join_date") or pts[0]["timestamp"]
    fills_now = ((stats or {}).get("all_time_pnl") or {}).get("trade_count") or (vol_fallback or {}).get("trade_count")
    trades_now = (stats or {}).get("trades")
    if trades_now is not None:
        # point-in-time: scale today's trade count by the share of fills that existed at `asof` (approximation)
        trades = trades_now * fills[-1] / fills_now if fills_now and fills[-1] < fills_now else trades_now
    else:
        trades = fills[-1]  # fallback: user-volume / series fill count (PROMPT §3)
    pnl, v = cum[-1], vol[-1]
    nz = [x for x in d if x != 0]
    sharpe = None
    if active >= 60 and len(d) > 2:
        m = sum(d) / len(d)
        sd = math.sqrt(sum((x - m) ** 2 for x in d) / (len(d) - 1))
        sharpe = m / sd * math.sqrt(365) if sd > 0 else None
    roll = [cum[i] - cum[i - 30] > 0 for i in range(30, len(cum))]
    pos = sum(x for x in d if x > 0)
    # max drawdown of cumulative PnL as a share of its highest level (PnL has no capital base)
    peak, mdd = -math.inf, 0.0
    for c in cum:
        peak = max(peak, c)
        mdd = max(mdd, peak - c)
    dd = mdd / peak if peak > 0 else 1.0
    return {
        "age_days": (asof - join) / 86400, "trades": trades, "fills": fills[-1], "pnl": pnl, "volume": v,
        "roi": pnl / v if v else None, "active_days": active, "sharpe": sharpe,
        "win_days": sum(1 for x in nz if x > 0) / len(nz) if nz else None,
        "roll30_pos": sum(roll) / len(roll) if roll else None,
        "top_day_share": max(d) / pos if pos > 0 else None, "drawdown": dd,
    }


def smart_score(f: dict | None) -> tuple[int, bool]:
    if not f:
        return 0, False
    gate = f["active_days"] >= 60 and (f["trades"] or 0) >= 100 and f["pnl"] >= 50_000
    s = 0
    s += 10 if f["age_days"] >= 120 else 0
    s += 10 if (f["trades"] or 0) >= 300 else 0
    s += 20 if f["pnl"] >= 25_000 else 0
    s += 10 if (f["roi"] or 0) >= 0.02 else 0
    s += 20 if (f["sharpe"] or 0) >= 1.5 else 0
    s += 10 if (f["win_days"] or 0) >= 0.55 else 0
    s += 10 if (f["roll30_pos"] or 0) >= 0.65 else 0
    s += 5 if f["top_day_share"] is not None and f["top_day_share"] <= 0.35 else 0
    s += 5 if f["drawdown"] <= 0.40 else 0
    return s, gate


def insider_score(f: dict | None, ticket: float, avg_price: float | None) -> int:
    if not f:
        return 0
    s = 0
    a = f["age_days"]
    s += 30 if a <= 7 else 20 if a <= 30 else 8 if a <= 90 else 0
    t = f["trades"] or 0
    s += 15 if t <= 20 else 8 if t <= 100 else 0
    share = ticket / f["volume"] if f["volume"] else 1.0
    s += 20 if share >= 0.5 else 10 if share >= 0.25 else 0
    s += 10 if ticket >= 10_000 else 5 if ticket >= 5_000 else 0
    s += 15 if ticket >= 3_000 and avg_price is not None and avg_price <= 0.30 else 0
    return s


TAG_ORDER = ["INSIDER-LIKE", "watch-fresh", "SMART", "solid", "MM/arb", "no-basis", "other"]


def tag_holder(f, amount, cost, avg_price, hedge) -> tuple[str, int, int]:
    """First hit wins (PROMPT §5). Returns (tag, insider_score, smart_score)."""
    ins = insider_score(f, cost, avg_price)
    sm, gate = smart_score(f)
    if amount >= 1000 and cost < 1:
        return "no-basis", ins, sm
    if hedge or (f and f["volume"] > 5_000_000 and f["roi"] is not None and abs(f["roi"]) < 0.005):
        return "MM/arb", ins, sm
    if cost >= 2000 and not hedge:
        if ins >= 60:
            return "INSIDER-LIKE", ins, sm
        if ins >= 40:
            return "watch-fresh", ins, sm
    if gate and sm >= 70:
        return "SMART", ins, sm
    if gate and sm >= 50:
        return "solid", ins, sm
    return "other", ins, sm


def load(name: str):
    return json.loads((OUT / name).read_text())


def save(name: str, obj) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text(json.dumps(obj, separators=(",", ":"), default=str))
