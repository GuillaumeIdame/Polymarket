"""Decoder tests built from a real OrderFilled log (Polygon, 2026-11-14), cross-checked vs Data API v2."""
from pmkt import chain

EX = "0xe111180000d2663c0091e4f400237545b87b996b"


def _log(side, token, maker_amt, taker_amt, fee, maker, taker, exchange=EX):
    words = [side, token, maker_amt, taker_amt, fee, 0, 0]
    return {
        "address": exchange, "blockNumber": hex(97305464), "blockTimestamp": hex(1794663644),
        "transactionHash": "0xabc", "logIndex": hex(7), "removed": False,
        "data": "0x" + "".join(f"{w:064x}" for w in words),
        "topics": [chain.SIG_ORDER_FILLED, "0x" + "11" * 32, "0x" + "0" * 24 + maker[2:], "0x" + "0" * 24 + taker[2:]],
    }


def test_signature_matches_live_topic0():
    assert chain.SIG_ORDER_FILLED == "0xd543adfd945773f1a62f74f0ee55a5e3b9b1a28262980ba90b1a89f2ea84d8ee"
    assert chain.SIG_FEE_CHARGED == "0x55bb3cade9d43b798a4fe5ffdd05024b2d7870df53920673bfc7e68047cd0ab1"


def test_taker_buy_row():
    # live example: taker BUY 32.258065 @ 0.62, fee 0.38 (sports rate 0.05)
    w = "0xadeaaadf9a0ccd6a34fb6a133839ef2643b97fd9"
    r = chain.decode_order_filled(_log(0, 123, 20_000_000, 32_258_065, 380_000, w, EX))
    assert r["role"] == "taker" and r["wallet"] == w and r["side"] == "BUY"
    assert abs(r["price"] - 0.62) < 1e-6 and abs(r["shares"] - 32.258065) < 1e-9
    assert abs(r["fee_usdc"] - 0.38) < 1e-9


def test_maker_sell_row():
    m, t = "0x" + "aa" * 20, "0x" + "bb" * 20
    r = chain.decode_order_filled(_log(1, 5, 10_000_000, 8_000_000, 0, m, t))
    assert r["role"] == "maker" and r["side"] == "SELL" and r["shares"] == 10.0 and r["usdc"] == 8.0
    assert abs(r["price"] - 0.8) < 1e-12


def test_transfer_batch_decoding():
    ids, vals = [11, 22], [1_000_000, 2_500_000]
    words = [64, 64 + 32 * 3, 2, *ids, 2, *vals]
    lg = {"address": "0x4d97", "blockNumber": "0x1", "blockTimestamp": "0x1", "transactionHash": "0x1",
          "logIndex": "0x0", "data": "0x" + "".join(f"{x:064x}" for x in words),
          "topics": [chain.SIG_TRANSFER_BATCH, "0x" + "0" * 64, "0x" + "0" * 64, "0x" + "0" * 24 + "cc" * 20]}
    rows = chain.decode_transfers(lg)
    assert [(r["token_id"], r["amount"]) for r in rows] == [("11", 1.0), ("22", 2.5)]
    assert rows[0]["from_addr"] == "0x" + "0" * 40
