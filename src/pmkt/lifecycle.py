"""Position lifecycle + resolution events for both position systems (CTF v1 and Polymarket Protocol V2).

Registry: src/pmkt/abi/events.json, built by scripts/build_event_registry.py from
github.com/Polymarket/polymarket-v2-external (commit recorded in the file). Every topic0 flagged
`observed_live` was matched against real Polygon logs, so the signature is confirmed on-chain.

What this covers (docs: resources/onchain-position-data):
  CTF            PositionSplit / PositionsMerge / PayoutRedemption / ConditionPreparation / ConditionResolution
  NegRiskAdapter PositionSplit / PositionsMerge / PositionsConverted / PayoutRedemption / MarketPrepared /
                 QuestionPrepared / OutcomeReported
  V2 modules     PositionsSplit / PositionsMerged / PositionRedeemed / ResultReported / ConditionResolved /
                 RemainingConditionsDerivableAsNo / SyntheticConditionDerivableAsYes / EventPrepared /
                 Migration* / PositionMigrated / LegacyCollateralSettled / Bridge* / combinatorial ops
  Router         Router* (call context only - NOT balance changes)
  AutoRedeemer   BinaryRedemption / NegRiskRedemption / Redemption (attribution only)
  OracleAggregator  request / proposal / report / dispute / resolution (UMA-dispute-risk monitoring)

Balances stay driven by ERC-1155 transfers (chain_transfers). Lifecycle rows explain *why* a transfer
happened (split vs trade vs redeem vs migration) and carry resolution results. Never add a lifecycle
amount to a balance on top of its transfer.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

import polars as pl

REGISTRY_PATH = Path(__file__).parent / "abi" / "events.json"
DEC = 1e6
AMOUNT_KEYS = {"amount", "payout", "value", "positionAmount", "collateralOut", "legacyPayout0", "legacyPayout1"}
WALLET_KEYS = ("initiator", "from", "stakeholder", "redeemer", "user", "resolver")
ROUTER_EVENTS_ARE_CONTEXT = True


@lru_cache(maxsize=1)
def registry() -> dict:
    return json.loads(REGISTRY_PATH.read_text())


@lru_cache(maxsize=1)
def contract_names() -> dict[str, str]:
    return {a.lower(): n for n, a in registry()["contracts"].items()}


def topics(observed_only: bool = False) -> list[str]:
    ev = registry()["events"]
    return [t for t, e in ev.items() if e["observed_live"] or not observed_only]


# ---------------------------------------------------------------- minimal ABI decoding (no eth_abi dep)

def _word(data: bytes, i: int) -> int:
    return int.from_bytes(data[32 * i: 32 * (i + 1)], "big")


def _static(typ: str, raw: int):
    if typ == "address":
        return "0x%040x" % (raw & ((1 << 160) - 1))
    if typ == "bool":
        return bool(raw)
    if typ.startswith("bytes"):
        n = int(typ[5:])  # left-aligned fixed bytes; bytes31/bytes29 keep native width
        return "0x" + raw.to_bytes(32, "big")[:n].hex()
    if typ.startswith("int"):
        return raw - (1 << 256) if raw >> 255 else raw  # sign-extended to 256 bits in ABI
    return raw  # uintN


def _is_dynamic(typ: str) -> bool:
    return typ.endswith("[]") or typ in ("bytes", "string")


def decode_log(lg: dict) -> tuple[str, dict] | None:
    """Return (event_name, args) or None if topic0 is not in the registry."""
    ev = registry()["events"].get(lg["topics"][0])
    if ev is None:
        return None
    inputs = ev["inputs"]
    if sum(i["indexed"] for i in inputs) != len(lg["topics"]) - 1:
        raise ValueError(f"indexed layout mismatch for {ev['signature']} in {lg['transactionHash']}")
    data = bytes.fromhex(lg["data"][2:])
    args, ti, wi = {}, 1, 0
    for k, inp in enumerate(inputs):
        name, typ = inp["name"] or f"arg{k}", inp["type"]
        if inp["indexed"]:
            raw = int(lg["topics"][ti], 16)
            ti += 1
            args[name] = ("0x%064x" % raw) if _is_dynamic(typ) or typ.startswith("(") else _static(typ, raw)
            continue
        if _is_dynamic(typ):
            off = _word(data, wi) // 32
            n = _word(data, off)
            if typ in ("bytes", "string"):
                b = data[32 * (off + 1): 32 * (off + 1) + n]
                args[name] = b.decode("utf-8", "replace") if typ == "string" else "0x" + b.hex()
            else:
                el = typ[:-2]
                args[name] = [_static(el, _word(data, off + 1 + j)) for j in range(n)]
        elif typ.startswith("("):
            comps = typ[1:-1].split(",")
            args[name] = [_static(c, _word(data, wi + j)) for j, c in enumerate(comps)]
            wi += len(comps) - 1
        else:
            args[name] = _static(typ, _word(data, wi))
        wi += 1
    return ev["name"], args


# ---------------------------------------------------------------- V2 identifiers (docs bit layout)

def decode_position_id(pid: int | str) -> dict:
    """[moduleId 8 | baseHash 128 | arity 16 | reserved 64 | resolutionChain 16 | conditionIndex 16 | outcomeIndex 8]"""
    p = int(pid)
    return {
        "module_id": p >> 248,
        "module": {1: "binary", 2: "negrisk", 3: "combinatorial"}.get(p >> 248, "unknown"),
        "base_hash": "0x%032x" % ((p >> 120) & ((1 << 128) - 1)),
        "arity": (p >> 104) & 0xFFFF,
        "resolution_chain": (p >> 24) & 0xFFFF,
        "condition_index": (p >> 8) & 0xFFFF,
        "outcome_index": p & 0xFF,
        "condition_id": "0x" + ((p >> 8) << 8).to_bytes(32, "big")[:31].hex(),   # bytes31
        "event_id": "0x" + ((p >> 24) << 24).to_bytes(32, "big")[:29].hex(),     # bytes29
    }


def is_v2_position_id(token_id: int | str) -> bool:
    """CTF token ids are keccak outputs; V2 ids carry module 1-3 in the top byte AND a zero reserved field."""
    p = int(token_id)
    return (p >> 248) in (1, 2, 3) and ((p >> 40) & ((1 << 64) - 1)) == 0


def complement_position_id(pid: int | str) -> str:
    """Other binary outcome: flip outcomeIndex 0<->1."""
    p = int(pid)
    return str((p & ~0xFF) | (1 - (p & 0xFF)))


# ---------------------------------------------------------------- normalised rows

LIFECYCLE_SCHEMA = {
    "block_number": pl.Int64, "block_ts": pl.Datetime("us", "UTC"), "tx_hash": pl.Utf8, "log_index": pl.Int64,
    "contract": pl.Utf8, "contract_name": pl.Utf8, "system": pl.Utf8, "event": pl.Utf8, "wallet": pl.Utf8,
    "condition_id": pl.Utf8, "position_id": pl.Utf8, "legacy_condition_id": pl.Utf8, "event_id": pl.Utf8,
    "outcome_index": pl.Int64, "amount": pl.Float64, "payout": pl.Float64, "result": pl.Utf8,
    "is_context_only": pl.Boolean, "args": pl.Utf8,
}


def _system(contract_name: str) -> str:
    if contract_name in ("conditional_tokens",):
        return "ctf"
    if contract_name == "negrisk_adapter":
        return "ctf_negrisk"
    return "v2" if contract_name else "unknown"


def normalise(lg: dict) -> dict | None:
    dec = decode_log(lg)
    if dec is None:
        return None
    name, a = dec
    cname = contract_names().get(lg["address"].lower(), "")
    wallet = next((a[k] for k in WALLET_KEYS if isinstance(a.get(k), str) and a[k].startswith("0x")
                   and len(a[k]) == 42), None)
    cond = a.get("conditionId") or a.get("v2ConditionId") or a.get("fullConditionId") or a.get("parentConditionId")
    pos = a.get("positionId") or a.get("newPositionId") or a.get("combinatorialPositionId")
    result = a.get("result") or a.get("payoutNumerators")
    if name == "OutcomeReported":
        result = [int(a["outcome"])]
    ts = lg.get("blockTimestamp")
    return {
        "block_number": int(lg["blockNumber"], 16),
        "block_ts": datetime.fromtimestamp(int(ts, 16), timezone.utc) if ts else None,
        "tx_hash": lg["transactionHash"], "log_index": int(lg["logIndex"], 16),
        "contract": lg["address"].lower(), "contract_name": cname, "system": _system(cname), "event": name,
        "wallet": wallet.lower() if wallet else None,
        "condition_id": cond if isinstance(cond, str) else None,
        "position_id": str(pos) if pos is not None else None,
        "legacy_condition_id": a.get("legacyConditionId"),
        "event_id": a.get("eventId") or a.get("marketId"),
        "outcome_index": a.get("outcomeIndex") if isinstance(a.get("outcomeIndex"), int) else None,
        "amount": a["amount"] / DEC if isinstance(a.get("amount"), int) else None,
        "payout": a["payout"] / DEC if isinstance(a.get("payout"), int) else None,
        "result": json.dumps(result) if result is not None else None,
        "is_context_only": cname in ("router_v2", "auto_redeemer_v2"),
        "args": json.dumps(a, default=str, separators=(",", ":")),
    }


def decode_lifecycle(lg: dict) -> list[dict]:
    r = normalise(lg)
    return [r] if r else []


def condition_map(lifecycle_rows: pl.DataFrame) -> pl.DataFrame:
    """Authoritative CTF -> V2 condition mapping from MigrationConditionRegistered (docs: treat as
    authoritative; never re-hash legacy ids). Columns: legacy_condition_id (bytes32), v2_condition_id (bytes31),
    module, registered_block."""
    return (lifecycle_rows.filter(pl.col("event") == "MigrationConditionRegistered")
            .select(legacy_condition_id="legacy_condition_id", v2_condition_id="condition_id",
                    module="contract_name", registered_block="block_number")
            .unique(subset=["legacy_condition_id"], keep="first"))
