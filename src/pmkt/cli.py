"""pmkt CLI. Run `pmkt -h`.

  pmkt gamma                         snapshot all open events/markets
  pmkt record --minutes 60           websocket L2/trade recorder (0 = forever)
  pmkt chain fills --blocks 1800     index exchange fills (resumes from cursor)
  pmkt chain transfers --blocks 300  index ERC-1155 transfers
  pmkt holders --top 50              holders+PnL snapshot for top markets by 24h volume
  pmkt status                        data inventory, coverage, gaps
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time

import polars as pl

from . import chain, dataapi, gamma, recorder, storage
from .config import load_settings
from .http import Http


def _http(s) -> Http:
    h = s["http"]
    return Http(h["timeout_s"], h["max_retries"], h["min_interval_s"])


def latest_markets(s) -> pl.DataFrame:
    con = storage.connect(s.data_dir)
    try:
        return storage.query(con, "SELECT * EXCLUDE (raw_json, description) FROM gamma_markets WHERE snapshot_ts = (SELECT max(snapshot_ts) FROM gamma_markets)")
    except Exception:  # noqa: BLE001
        return gamma.snapshot_markets(_http(s), s.endpoint("gamma"), s.data_dir)


def cmd_gamma(s, a):
    df = gamma.snapshot_markets(_http(s), s.endpoint("gamma"), s.data_dir, closed=a.closed, full=a.full)
    print(df.group_by("category").agg(n=pl.len(), vol24h=pl.col("volume_24h").sum()).sort("vol24h", descending=True))


def cmd_record(s, a):
    def universe():
        m = gamma.universe_candidates(_http(s), s.endpoint("gamma"), s["recorder"]["universe"])
        storage.write_parquet(s.data_dir, "recorder_universe", m)  # what we chose to record, as-of
        u = recorder.select_universe(m, s["recorder"]["universe"])
        return set(x for ids in u["asset_ids"].to_list() for x in ids)
    res = asyncio.run(recorder.record(s, universe, duration_s=a.minutes * 60 or None, refresh_min=a.refresh))
    print(json.dumps(res))


def cmd_chain(s, a):
    while True:
        res = chain.index_stream(s, _http(s), a.stream, from_block=a.from_block,
                                 lookback_blocks=a.blocks, max_batches=a.max_batches)
        print(json.dumps(res, default=str), flush=True)
        if not a.follow:
            return
        a.from_block = None  # resume from cursor
        time.sleep(a.follow)


def cmd_holders(s, a):
    while True:
        _holders_once(s, a)
        if not a.every:
            return
        time.sleep(a.every * 60)


def _holders_once(s, a):
    m = latest_markets(s).filter(~pl.col("closed")).sort("volume_24h", descending=True, nulls_last=True).head(a.top)
    df = dataapi.snapshot_holders(_http(s), s.endpoint("data_api"), s.data_dir, m["condition_id"].to_list())
    print(f"{df.height} holder rows for {m.height} markets")


def cmd_book(s, a):
    """Window position book for wallets from the indexed lake (fills + transfers + lifecycle)."""
    from . import ledger, lifecycle
    con = storage.connect(s.data_dir)
    wl = ",".join(f"'{w.lower()}'" for w in a.wallet)
    fills = storage.query(con, f"select * from chain_fills where wallet in ({wl})")
    tr = storage.query(con, f"select * from chain_transfers where from_addr in ({wl}) or to_addr in ({wl})")
    lc = storage.query(con, f"select * from chain_lifecycle where tx_hash in (select tx_hash from chain_transfers "
                            f"where from_addr in ({wl}) or to_addr in ({wl}))")
    prot = list(s["chain"]["exclude_from_holders"]) + list(lifecycle.registry()["contracts"].values())
    led = ledger.build_ledger(fills, tr, lc, {w.lower() for w in a.wallet}, prot)
    book, flags = ledger.replay(led)
    print(led.group_by("kind").len().sort("len", descending=True))
    print(book.frame().sort("cost_basis", descending=True))
    if flags:
        print("flags:", {f: flags.count(f) for f in set(flags)})


def cmd_status(s, a):
    con = storage.connect(s.data_dir)
    views = [r[0] for r in con.execute("SELECT view_name FROM duckdb_views() WHERE NOT internal").fetchall()]
    tcol = {"gamma_markets": "snapshot_ts", "api_holders": "snapshot_ts", "ws_market": "recv_ts",
            "chain_fills": "block_ts", "chain_transfers": "block_ts", "ws_sessions": "opened"}
    for v in views:
        c = tcol.get(v)
        q = f"SELECT '{v}' AS dataset, count(*) AS n{', min(' + c + ') AS t_min, max(' + c + ') AS t_max' if c else ''} FROM {v}"
        print(storage.query(con, q).to_dicts()[0])
    if "chain_fills" in views:
        print("fill gaps (>30 blocks):")
        print(chain.detect_gaps(con))
    if "ws_sessions" in views:
        print(storage.query(con, "SELECT reason, count(*) n FROM ws_sessions GROUP BY 1 ORDER BY 2 DESC"))


def main(argv=None):
    p = argparse.ArgumentParser(prog="pmkt", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config")
    p.add_argument("-v", "--verbose", action="store_true")
    sp = p.add_subparsers(dest="cmd", required=True)
    g = sp.add_parser("gamma")
    g.add_argument("--closed", action="store_true")
    g.add_argument("--full", action="store_true", help="daily full snapshot incl. raw_json")
    r = sp.add_parser("record")
    r.add_argument("--minutes", type=float, default=0)
    r.add_argument("--refresh", type=float, default=30, help="universe refresh, minutes")
    c = sp.add_parser("chain")
    c.add_argument("stream", choices=list(chain.STREAMS))
    c.add_argument("--blocks", type=int, default=1800, help="lookback if no cursor (~1h = 1800 blocks)")
    c.add_argument("--from-block", type=int)
    c.add_argument("--max-batches", type=int)
    c.add_argument("--follow", type=float, default=0, help="keep indexing; sleep N seconds between passes")
    h = sp.add_parser("holders")
    h.add_argument("--top", type=int, default=50)
    h.add_argument("--every", type=float, default=0, help="repeat every N minutes")
    sp.add_parser("status")
    bk = sp.add_parser("book", help="window position book from fills+transfers+lifecycle")
    bk.add_argument("wallet", nargs="+")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    s = load_settings(a.config)
    {"gamma": cmd_gamma, "record": cmd_record, "chain": cmd_chain, "holders": cmd_holders,
     "status": cmd_status, "book": cmd_book}[a.cmd](s, a)


if __name__ == "__main__":
    main()
