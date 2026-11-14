"""V2/CTF lifecycle decoding against REAL Polygon logs (one per topic, captured 2026-11-14)."""
import json
from pathlib import Path

import pytest

from pmkt import chain
from pmkt import lifecycle as lc

LOGS = json.loads((Path(__file__).parent / "fixtures" / "live_logs_2026-11-14.json").read_text())
SKIP = {chain.SIG_TRANSFER_SINGLE, chain.SIG_TRANSFER_BATCH, chain.SIG_ORDER_FILLED, chain.SIG_FEE_CHARGED,
        "0x17307eab39ab6107e8899845ad3d59bd9653f200f220920489ca2b5937696c31",  # ApprovalForAll
        "0x174b3811690657c217184f89418266767c87e4805d09680c39fc9c031c0cab7c"}  # OrdersMatched


def by_name(name):
    for lg in LOGS:
        d = lc.decode_log(lg)
        if d and d[0] == name:
            return lg, d[1]
    raise KeyError(name)


def test_every_live_lifecycle_topic_is_in_registry_and_decodes():
    for lg in LOGS:
        if lg["topics"][0] in SKIP:
            continue
        r = lc.normalise(lg)
        assert r is not None, lg["topics"][0]
        assert lc.registry()["events"][lg["topics"][0]]["observed_live"]


def test_position_migrated_ids_consistent():
    _, a = by_name("PositionMigrated")
    d = lc.decode_position_id(a["positionId"])
    assert d["condition_id"] == a["conditionId"] and d["outcome_index"] == a["outcomeIndex"]
    assert len(a["conditionId"]) == 2 + 62  # bytes31 kept at native width
    assert d["module"] in ("binary", "negrisk")


def test_migration_resolved_ppm():
    _, a = by_name("MigrationResolved")
    tot = a["legacyPayout0"] + a["legacyPayout1"]
    assert a["result0"] + a["result1"] == 1_000_000
    assert abs(a["result0"] - 1e6 * a["legacyPayout0"] / tot) <= 1


def test_dynamic_arrays_and_bytes():
    _, a = by_name("ConditionResolution")          # CTF: uint256[] payoutNumerators
    assert isinstance(a["payoutNumerators"], list) and len(a["payoutNumerators"]) == a["outcomeSlotCount"]
    _, q = by_name("QuestionPrepared")              # NegRiskAdapter: bytes data (UMA ancillary text)
    assert q["data"].startswith("0x") and len(q["data"]) > 10
    _, c = by_name("CombinatorialConditionPrepared")
    assert c["legs"] and all(lc.is_v2_position_id(x) for x in c["legs"])  # legs are binary/negrisk V2 ids


def test_router_and_autoredeemer_are_context_only():
    for name in ("RouterPositionRedeemed", "BinaryRedemption"):
        lg, _ = by_name(name)
        assert lc.normalise(lg)["is_context_only"]


def test_position_id_layout_roundtrip():
    module, base, arity, chain_id, cidx, oidx = 2, (1 << 127) | 12345, 7, 137, 3, 1
    pid = (module << 248) | (base << 120) | (arity << 104) | (chain_id << 24) | (cidx << 8) | oidx
    d = lc.decode_position_id(pid)
    assert (d["module"], d["arity"], d["resolution_chain"], d["condition_index"], d["outcome_index"]) == \
        ("negrisk", 7, 137, 3, 1)
    assert int(d["condition_id"], 16) << 8 == pid & ~0xFF
    assert int(d["event_id"], 16) << 24 == pid & ~0xFFFFFF
    assert int(lc.complement_position_id(pid)) & 0xFF == 0
    assert lc.is_v2_position_id(pid)


def test_ctf_token_ids_not_classified_v2():
    ctf_ids = ["74979179581704693173891779115584035051993241104205845957775234004833817931168",
               "32017896468660101089348809996836868536053704676247704599557402601839845523297"]
    assert not any(lc.is_v2_position_id(t) for t in ctf_ids)


def test_layout_mismatch_raises():
    lg, _ = by_name("PositionMigrated")
    with pytest.raises(ValueError):
        lc.decode_log({**lg, "topics": lg["topics"][:-1]})
