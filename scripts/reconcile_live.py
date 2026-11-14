"""Forward reconciliation of the chain ledger/position book vs on-chain balances and Data API v2.

Public RPCs only serve recent state (~128 blocks), so we reconcile forward:
  open : pick active wallets, record block B0, chain balanceOf(B0) for all their positions,
         Data API /v2/positions (size, avg_price, realized_pnl) at T0.
  close: index fills/transfers/lifecycle over (B0, B1], chain balanceOf(B1), API positions at T1,
         seed the ledger with the API's opening book, replay the window and compare.

Usage:
  python scripts/reconcile_live.py open  [n_wallets]
  python scripts/reconcile_live.py close
State/output: {data_dir}/recon/
"""
from __future__ import annotations

import json
import random
import sys
import time

import polars as pl
from eth_hash.auto import keccak

from pmkt import chain, lifecycle, storage
from pmkt.config import load_settings
from pmkt.http import Http

CTF = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
PM = "0x006F54F7f9A22e0000CC2AB60031000000ae9fEF"
SEL_BATCH = "0x" + keccak(b"balanceOfBatch(address[],uint256[])").hex()[:8]
API = "https://data-api.polymarket.com/v2"


def balance_batch(rpc: chain.Rpc, contract: str, pairs: list[tuple[str, str]], block: int) -> list[float]:
    out = []
    for i in range(0, len(pairs), 150):
        chunk = pairs[i:i + 150]
        n = len(chunk)
        head = (64).to_bytes(32, "big") + (64 + 32 * (n + 1)).to_bytes(32, "big")
        addrs = n.to_bytes(32, "big") + b"".join(bytes.fromhex(a[2:]).rjust(32, b"\0") for a, _ in chunk)
        ids = n.to_bytes(32, "big") + b"".join(int(t).to_bytes(32, "big") for _, t in chunk)
        res = rpc.call("eth_call", [{"to": contract, "data": SEL_BATCH + (head + addrs + ids).hex()}, hex(block)])
        b = bytes.fromhex(res[2:])
        cnt = int.from_bytes(b[32:64], "big")
        out += [int.from_bytes(b[64 + 32 * k: 96 + 32 * k], "big") / 1e6 for k in range(cnt)]
    return out


def chain_balances(rpc, pairs, block) -> pl.DataFrame:
    v2 = [p for p in pairs if lifecycle.is_v2_position_id(p[1])]
    v1 = [p for p in pairs if not lifecycle.is_v2_position_id(p[1])]
    rows = []
    for contract, ps in ((CTF, v1), (PM, v2)):
        if ps:
            rows += [{"wallet": w, "token_id": t, "balance": b}
                     for (w, t), b in zip(ps, balance_batch(rpc, contract, ps, block), strict=True)]
    return pl.DataFrame(rows, schema={"wallet": pl.Utf8, "token_id": pl.Utf8, "balance": pl.Float64})


def api_positions(http: Http, wallet: str, max_rows: int = 400) -> list[dict] | None:
    """All lifecycle statuses for one wallet; None if the book is bigger than max_rows (skip MMs)."""
    keep = ("token_id", "condition_id", "current_size", "avg_price", "total_cost_usdc", "entry_cost_usdc",
            "realized_pnl", "status", "first_entry_at", "last_event_at")
    seen, out = set(), []
    for status in ("OPEN", "REDEEMABLE", "REDEEMABLE_LOST", "MERGEABLE"):
        body = http.get(f"{API}/positions", {"user": wallet, "limit": 500, "status": status})
        if (body.get("pagination") or {}).get("has_more"):
            return None
        for r in body.get("data") or []:
            if r["token_id"] not in seen:
                seen.add(r["token_id"])
                out.append({"wallet": wallet, **{k: r.get(k) for k in keep}})
        if len(out) > max_rows:
            return None
    return out


def _fetch_wallet(args):
    """Bracket the API read with chain heads: positions reflect state somewhere in [start, end]."""
    s, wl = args
    http = Http(min_interval_s=0.05)
    rpc = chain.Rpc(http, s.endpoint("polygon_rpc"))
    start = rpc.head()
    rows = api_positions(http, wl)
    end = rpc.head()
    return wl, rows, start, end


def do_open(s, n_wallets: int):
    from concurrent.futures import ThreadPoolExecutor
    http, out = Http(), s.data_dir / "recon"
    out.mkdir(exist_ok=True)
    rpc = chain.Rpc(http, s.endpoint("polygon_rpc"))
    con = storage.connect(s.data_dir)
    excl = {a.lower() for a in s["chain"]["exclude_from_holders"]}
    w = storage.query(con, "select wallet, count(*) n from chain_fills group by 1 having n between 3 and 400")
    cands = [x for x in w["wallet"].to_list() if x not in excl]
    random.Random(7).shuffle(cands)
    cands = cands[: int(n_wallets * 1.6)]
    with ThreadPoolExecutor(8) as ex:
        res = list(ex.map(_fetch_wallet, [(s, c) for c in cands]))
    res = [r for r in res if r[1] is not None][:n_wallets]
    wallets = [r[0] for r in res]
    fetch = {r[0]: {"start": r[2], "end": r[3]} for r in res}
    api_rows = [x for r in res for x in r[1]]
    b0 = rpc.head() - s["chain"]["confirmations"]
    api0 = pl.DataFrame(api_rows, infer_schema_length=None)
    bal0 = chain_balances(rpc, [(r["wallet"], r["token_id"]) for r in api_rows], b0)
    api0.write_parquet(out / "api_t0.parquet")
    bal0.write_parquet(out / "chain_b0.parquet")
    (out / "state.json").write_text(json.dumps({"b0": b0, "t0": time.time(), "wallets": wallets, "fetch": fetch}))
    lo = min(v["start"] for v in fetch.values())
    print(f"open: B0={b0}, wallets={len(wallets)} (of {len(cands)} tried), api positions={api0.height}, "
          f"chain pairs={bal0.height} (nonzero {bal0.filter(pl.col('balance') > 0).height}), "
          f"api fetch blocks {lo}..{max(v['end'] for v in fetch.values())}")


def do_close(s):
    from pmkt import ledger
    http, out = Http(), s.data_dir / "recon"
    st = json.loads((out / "state.json").read_text())
    b0, wallets, fetch = st["b0"], st["wallets"], st["fetch"]
    lo = min(v["start"] for v in fetch.values())
    rpc = chain.Rpc(http, s.endpoint("polygon_rpc"))
    b1 = rpc.head() - s["chain"]["confirmations"]
    for stream in ("fills", "transfers", "lifecycle"):
        print(chain.index_stream(s, http, stream, from_block=lo, to_block=b1))
    api1 = pl.DataFrame([r for w in wallets for r in (api_positions(http, w, 10**6) or [])], infer_schema_length=None)
    con = storage.connect(s.data_dir)
    wl = ",".join(f"'{w}'" for w in wallets)
    win = f"block_number >= {lo} and block_number <= {b1}"
    fills = storage.query(con, f"select * from chain_fills where {win} and wallet in ({wl})")
    tr = storage.query(con, f"select * from chain_transfers where {win} and (from_addr in ({wl}) or to_addr in ({wl}))")
    lc = storage.query(con, f"select * from chain_lifecycle where {win} and tx_hash in "
                            f"(select distinct tx_hash from chain_transfers where {win} and "
                            f"(from_addr in ({wl}) or to_addr in ({wl})))")
    api0 = pl.read_parquet(out / "api_t0.parquet")
    pairs = sorted({(r["wallet"], r["token_id"]) for r in api0.iter_rows(named=True)}
                   | {(r["wallet"], r["token_id"]) for r in api1.iter_rows(named=True)}
                   | {(a, t) for a, t in tr.select("to_addr", "token_id").iter_rows() if a in set(wallets)}
                   | {(a, t) for a, t in tr.select("from_addr", "token_id").iter_rows() if a in set(wallets)})
    bal1 = chain_balances(rpc, pairs, b1)
    bal0 = pl.read_parquet(out / "chain_b0.parquet")
    api1.write_parquet(out / "api_t1.parquet")
    bal1.write_parquet(out / "chain_b1.parquet")

    excl = list(s["chain"]["exclude_from_holders"]) + list(lifecycle.registry()["contracts"].values())
    winners = set(api0.filter(pl.col("status") == "REDEEMABLE")["token_id"].to_list())
    led = ledger.build_ledger(fills, tr, lc, wallets=set(wallets), protocol=excl, winners=winners)
    led.write_parquet(out / "ledger.parquet")
    rep = ledger.reconcile_window(led, api0, api1, bal0, bal1, b0, fetch)
    for k, v in rep["summary"].items():
        print(f"{k}: {v}")
    for name, df in rep["frames"].items():
        df.write_parquet(out / f"recon_{name}.parquet")
    (out / "summary.json").write_text(json.dumps({"b0": b0, "b1": b1, **rep["summary"]}, indent=1, default=str))


if __name__ == "__main__":
    s = load_settings()
    if sys.argv[1] == "open":
        do_open(s, int(sys.argv[2]) if len(sys.argv) > 2 else 150)
    else:
        do_close(s)
