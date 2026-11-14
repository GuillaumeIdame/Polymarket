"""Polygon on-chain indexer: exchange fills (OrderFilled, FeeCharged) and ERC-1155 transfers.

Event ABIs were matched against live logs on 2026-11-14 (topic0 = keccak of the signature):
  OrderFilled(bytes32 indexed orderHash, address indexed maker, address indexed taker,
              uint8 side, uint256 tokenId, uint256 makerAmountFilled, uint256 takerAmountFilled,
              uint256 fee, bytes32 builder, bytes32 metadata)
  - one row per order filled; `maker` is the ORDER OWNER (the wallet whose order filled).
  - the taker's own order appears as a row with taker == exchange address (role = "taker").
  - side 0 = BUY: makerAmount = collateral paid, takerAmount = shares received.
    side 1 = SELL: makerAmount = shares given, takerAmount = collateral received.
  - collateral (pUSD/USDC) and shares both have 6 decimals.
  - verified vs Data API /v2/trades on the same tx hashes (price, size, side, fee all match).
  TransferSingle / TransferBatch: standard ERC-1155.

Cursor: state/chain_<stream>.json holds the last fully processed block; each batch writes one
immutable Parquet file and only then advances the cursor (crash-safe, at-least-once; dedupe on
(tx_hash, log_index) downstream).
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

import polars as pl
from eth_hash.auto import keccak

from .http import Http
from .storage import Cursor, write_parquet

log = logging.getLogger(__name__)

DEC = 1e6
ZERO = "0x" + "0" * 40


def topic0(sig: str) -> str:
    return "0x" + keccak(sig.encode()).hex()


SIG_ORDER_FILLED = topic0(
    "OrderFilled(bytes32,address,address,uint8,uint256,uint256,uint256,uint256,bytes32,bytes32)")
SIG_FEE_CHARGED = topic0("FeeCharged(address,uint256)")
SIG_TRANSFER_SINGLE = topic0("TransferSingle(address,address,address,uint256,uint256)")
SIG_TRANSFER_BATCH = topic0("TransferBatch(address,address,address,uint256[],uint256[])")


def _words(data: str) -> list[int]:
    h = data[2:]
    return [int(h[i:i + 64], 16) for i in range(0, len(h), 64)]


def _addr(topic: str) -> str:
    return "0x" + topic[-40:].lower()


def _ts(log_: dict) -> datetime | None:
    t = log_.get("blockTimestamp")
    return datetime.fromtimestamp(int(t, 16), timezone.utc) if t else None


def decode_order_filled(lg: dict) -> dict:
    w = _words(lg["data"])
    side, token_id, maker_amt, taker_amt, fee, builder, metadata = w[:7]
    exchange = lg["address"].lower()
    maker, taker = _addr(lg["topics"][2]), _addr(lg["topics"][3])
    if side == 0:  # BUY
        usdc, shares = maker_amt / DEC, taker_amt / DEC
    else:  # SELL
        shares, usdc = maker_amt / DEC, taker_amt / DEC
    return {
        "block_number": int(lg["blockNumber"], 16),
        "block_ts": _ts(lg),
        "tx_hash": lg["transactionHash"],
        "log_index": int(lg["logIndex"], 16),
        "exchange": exchange,
        "order_hash": lg["topics"][1],
        "wallet": maker,                       # order owner
        "counterparty": taker,
        "role": "taker" if taker == exchange else "maker",
        "side": "BUY" if side == 0 else "SELL",
        "token_id": str(token_id),
        "shares": shares,
        "usdc": usdc,
        "price": usdc / shares if shares else None,
        "fee_usdc": fee / DEC,
        "builder": "0x%064x" % builder,
        "metadata": "0x%064x" % metadata,
    }


def decode_transfers(lg: dict) -> list[dict]:
    t0 = lg["topics"][0]
    base = {
        "block_number": int(lg["blockNumber"], 16),
        "block_ts": _ts(lg),
        "tx_hash": lg["transactionHash"],
        "log_index": int(lg["logIndex"], 16),
        "contract": lg["address"].lower(),
        "operator": _addr(lg["topics"][1]),
        "from_addr": _addr(lg["topics"][2]),
        "to_addr": _addr(lg["topics"][3]),
    }
    w = _words(lg["data"])
    if t0 == SIG_TRANSFER_SINGLE:
        return [{**base, "batch_idx": 0, "token_id": str(w[0]), "amount": w[1] / DEC}]
    # TransferBatch: data = offset_ids, offset_vals, len_ids, ids..., len_vals, vals...
    off_ids, off_vals = w[0] // 32, w[1] // 32
    n = w[off_ids]
    ids = w[off_ids + 1: off_ids + 1 + n]
    vals = w[off_vals + 1: off_vals + 1 + n]
    return [{**base, "batch_idx": i, "token_id": str(t), "amount": v / DEC}
            for i, (t, v) in enumerate(zip(ids, vals, strict=True))]


FILL_SCHEMA = {
    "block_number": pl.Int64, "block_ts": pl.Datetime("us", "UTC"), "tx_hash": pl.Utf8,
    "log_index": pl.Int64, "exchange": pl.Utf8, "order_hash": pl.Utf8, "wallet": pl.Utf8,
    "counterparty": pl.Utf8, "role": pl.Utf8, "side": pl.Utf8, "token_id": pl.Utf8,
    "shares": pl.Float64, "usdc": pl.Float64, "price": pl.Float64, "fee_usdc": pl.Float64,
    "builder": pl.Utf8, "metadata": pl.Utf8,
}
TRANSFER_SCHEMA = {
    "block_number": pl.Int64, "block_ts": pl.Datetime("us", "UTC"), "tx_hash": pl.Utf8,
    "log_index": pl.Int64, "contract": pl.Utf8, "operator": pl.Utf8, "from_addr": pl.Utf8,
    "to_addr": pl.Utf8, "batch_idx": pl.Int64, "token_id": pl.Utf8, "amount": pl.Float64,
}


class Rpc:
    def __init__(self, http: Http, url: str):
        self.http, self.url = http, url

    def call(self, method: str, params: list):
        body = self.http.post(self.url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        if "error" in body:
            raise RuntimeError(f"rpc error {method}: {body['error']}")
        return body["result"]

    def head(self) -> int:
        return int(self.call("eth_blockNumber", []), 16)

    def logs(self, addresses: list[str], topics: list[str], frm: int, to: int) -> list[dict]:
        return self.call("eth_getLogs", [{"address": addresses, "topics": [topics],
                                          "fromBlock": hex(frm), "toBlock": hex(to)}])


STREAMS = {
    # stream -> (contract kind, topic0 list, decoder, dataset, schema)
    "fills": ("exchange", [SIG_ORDER_FILLED], lambda lg: [decode_order_filled(lg)], "chain_fills",
              FILL_SCHEMA),
    "lifecycle": ("lifecycle", None, None, "chain_lifecycle", None),  # filled lazily below
    "transfers": ("erc1155", [SIG_TRANSFER_SINGLE, SIG_TRANSFER_BATCH], decode_transfers,
                  "chain_transfers", TRANSFER_SCHEMA),
}


def _fetch_range(rpc: Rpc, addrs, topics, frm: int, to: int) -> list[dict]:
    """getLogs with automatic range halving when the provider caps the response."""
    try:
        return rpc.logs(addrs, topics, frm, to)
    except Exception as e:  # noqa: BLE001 - providers signal "too many results" in many ways
        if to <= frm:
            raise
        mid = (frm + to) // 2
        log.info("splitting %d-%d (%s)", frm, to, str(e)[:120])
        return _fetch_range(rpc, addrs, topics, frm, mid) + _fetch_range(rpc, addrs, topics, mid + 1, to)


def index_stream(settings, http: Http, stream: str, from_block: int | None = None,
                 to_block: int | None = None, lookback_blocks: int = 1800, max_batches: int | None = None
                 ) -> dict:
    """Index [cursor+1 .. to_block] for one stream. Returns stats incl. gaps detected."""
    kind, topics, decoder, dataset, schema = STREAMS[stream]
    if stream == "lifecycle":
        from . import lifecycle as lc
        topics, decoder, schema = lc.topics(), lc.decode_lifecycle, lc.LIFECYCLE_SCHEMA
    ccfg = settings["chain"]
    addrs = [a for a, k in ccfg["contracts"].values() if k == kind]
    rpc = Rpc(http, settings.endpoint("polygon_rpc"))
    cur = Cursor(settings.data_dir, f"chain_{stream}")
    head = rpc.head() - ccfg["confirmations"]
    to_block = min(to_block or head, head)
    state = cur.get({})
    if from_block is None:
        from_block = state["last_block"] + 1 if state.get("last_block") else to_block - lookback_blocks
    step = ccfg["blocks_per_request"]
    stats = {"stream": stream, "from": from_block, "to": to_block, "rows": 0, "files": 0, "batches": 0,
             "empty_batches": 0}
    b = from_block
    t0 = time.time()
    while b <= to_block:
        e = min(b + step - 1, to_block)
        logs = _fetch_range(rpc, addrs, topics, b, e)
        rows = [r for lg in logs if not lg.get("removed") for r in decoder(lg)]
        df = pl.DataFrame(rows, schema=schema) if rows else pl.DataFrame(schema=schema)
        if df.height:
            ts = df["block_ts"].min()
            write_parquet(settings.data_dir, dataset, df, ts, tag=f"b{b}-{e}")
            stats["files"] += 1
        else:
            stats["empty_batches"] += 1  # an empty batch on exchanges is itself a data-quality flag
        stats["rows"] += df.height
        stats["batches"] += 1
        cur.set({"last_block": e, "dataset": dataset})
        b = e + 1
        if max_batches and stats["batches"] >= max_batches:
            break
    stats["last_block"] = b - 1
    stats["seconds"] = round(time.time() - t0, 1)
    log.info("chain %s: %s", stream, stats)
    return stats


def detect_gaps(con, dataset: str = "chain_fills") -> pl.DataFrame:
    from .storage import query
    """Block ranges with no rows. On the exchanges a >30-block silent stretch is a probable gap."""
    return query(con, f"""
        WITH b AS (SELECT DISTINCT block_number FROM {dataset}),
        g AS (SELECT block_number, block_number - LAG(block_number) OVER (ORDER BY block_number) AS d FROM b)
        SELECT block_number - d AS gap_start, block_number AS gap_end, d AS blocks
        FROM g WHERE d > 30 ORDER BY d DESC
    """)


COLLATERAL_TOKENS = {
    "0xc011a7e12a19f7b1f670d46f03b03f3342e82dfb",  # pUSD (Polymarket v2 collateral)
    "0x2791bca1f2de4661ed88a30c99a7a9449aa84174",  # USDC.e (bridged; CTF collateral)
    "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359",  # USDC (native)
}
SIG_ERC20_TRANSFER = topic0("Transfer(address,address,uint256)")


def collateral_received(rpc: Rpc, txs: list[str], wallets: set[str], workers: int = 8) -> dict[tuple[str, str], float]:
    """(tx, wallet) -> net collateral received in that tx, from receipts (ERC-20 Transfer of pUSD/USDC)."""
    from concurrent.futures import ThreadPoolExecutor
    out: dict[tuple[str, str], float] = {}

    def one(tx):
        rc = rpc.call("eth_getTransactionReceipt", [tx])
        res = {}
        for lg in rc["logs"]:
            if lg["address"].lower() in COLLATERAL_TOKENS and lg["topics"][0] == SIG_ERC20_TRANSFER \
                    and len(lg["topics"]) == 3:
                frm, to, amt = _addr(lg["topics"][1]), _addr(lg["topics"][2]), int(lg["data"], 16) / DEC
                if to in wallets:
                    res[(tx, to)] = res.get((tx, to), 0.0) + amt
                if frm in wallets:
                    res[(tx, frm)] = res.get((tx, frm), 0.0) - amt
        return res
    with ThreadPoolExecutor(workers) as ex:
        for r in ex.map(one, txs):
            out.update(r)
    return out
