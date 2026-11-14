"""Build src/pmkt/abi/events.json from a checkout of github.com/Polymarket/polymarket-v2-external.

Sources: compiled ABIs in artifacts/ (CTF, NegRiskAdapter, UMA adapters) + `event` declarations
parsed from the V2 Solidity sources, with user-defined value types / enums / structs canonicalised.
Every event is kept; `observed_live` marks the ones whose topic0 was seen in live Polygon logs
(i.e. signature confirmed on-chain, not just in source). Pass the observed-topics JSON as argv[2].

Usage: python scripts/build_event_registry.py /path/to/polymarket-v2-external
"""
import glob
import json
import os
import re
import subprocess
import sys

from eth_hash.auto import keccak

# contract name -> topic0s seen in live logs (2026-11-14, 40k-block window; CTF 2k blocks)
OBSERVED_CONTRACTS = {
    "conditional_tokens": "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045",
    "negrisk_adapter": "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296",
    "position_manager_v2": "0x006F54F7f9A22e0000CC2AB60031000000ae9fEF",
    "router_v2": "0x12121212006e4CD160D18e3f00711DA5c3372600",
    "binary_module_v2": "0x1000008dD9001B968442c1000017eaE6E0dA00Ba",
    "negrisk_module_v2": "0x200000900045e3B6259600682756002200028933",
    "combinatorial_module_v2": "0x30000034706C7d8e12009DAB006Be20000c031A8",
    "auto_redeemer_v2": "0xa1200000d0002264C9a1698e001292D00E1b00af",
    "oracle_aggregator_v2": "0x0A0a0A0A8B00C51b7D810501b03F230028C04a87",
}
SKIP = {"TransferSingle", "TransferBatch", "ApprovalForAll", "OrderFilled", "OrdersMatched", "FeeCharged"}


def topic(sig):
    return "0x" + keccak(sig.encode()).hex()


def main(root):
    out = {}

    def add(name, inputs, origin):
        sig = f"{name}({','.join(i['type'] for i in inputs)})"
        e = out.setdefault(topic(sig), {"name": name, "signature": sig, "inputs": inputs, "origin": origin})
        if [i["indexed"] for i in e["inputs"]] != [i["indexed"] for i in inputs]:
            # same signature, different indexed layout in another contract: keep every variant
            alts = e.setdefault("variants", [])
            if all([i["indexed"] for i in a] != [i["indexed"] for i in inputs] for a in alts):
                alts.append(inputs)

    def canon_json(t):
        if t["type"].startswith("tuple"):
            return "(" + ",".join(canon_json(c) for c in t["components"]) + ")" + t["type"][5:]
        return t["type"]

    for f in glob.glob(os.path.join(root, "artifacts", "*.json")):
        d = json.load(open(f))
        for e in d.get("abi", d) if isinstance(d, dict) else d:
            if isinstance(e, dict) and e.get("type") == "event":
                add(e["name"], [{"name": i["name"], "type": canon_json(i), "indexed": i["indexed"]}
                                for i in e["inputs"]], os.path.relpath(f, root))

    txt = "\n".join(open(f).read() for f in glob.glob(os.path.join(root, "**/*.sol"), recursive=True)
                    if "/lib/" not in f and "test" not in f.lower())
    txt = re.sub(r"//[^\n]*|/\*.*?\*/", "", txt, flags=re.S)
    udvt = dict(re.findall(r"\btype\s+(\w+)\s+is\s+(\w+)\s*;", txt))
    enums = set(re.findall(r"\benum\s+(\w+)", txt))
    structs = {n: [x.strip().split()[0] for x in b.split(";") if x.strip()]
               for n, b in re.findall(r"\bstruct\s+(\w+)\s*\{(.*?)\}", txt, flags=re.S)}

    def canon(t):
        m = re.match(r"([\w.]+)((?:\[\d*\])*)$", t)
        base, arr = m.group(1).split(".")[-1], m.group(2)
        if base in udvt:
            base = udvt[base]
        elif base in enums:
            base = "uint8"
        elif base in structs:
            base = "(" + ",".join(canon(x) for x in structs[base]) + ")"
        elif base in ("uint", "int"):
            base += "256"
        return base + arr

    for name, args in re.findall(r"\bevent\s+(\w+)\s*\((.*?)\)\s*;", txt, flags=re.S):
        inputs = []
        for p in [p.strip() for p in args.split(",") if p.strip()]:
            toks = p.split()
            inputs.append({"name": toks[-1] if len(toks) > 1 and toks[-1] != "indexed" else "",
                           "type": canon(toks[0]), "indexed": "indexed" in toks})
        add(name, inputs, "v2 solidity")

    commit = subprocess.run(["git", "-C", root, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    observed = json.load(open(sys.argv[2])) if len(sys.argv) > 2 else None
    events = {t: {**e, "observed_live": observed is not None and t in observed}
              for t, e in out.items() if e["name"] not in SKIP}
    reg = {"source": "github.com/Polymarket/polymarket-v2-external", "commit": commit,
           "contracts": OBSERVED_CONTRACTS, "events": dict(sorted(events.items(), key=lambda kv: kv[1]["name"]))}
    dst = os.path.join(os.path.dirname(__file__), "..", "src", "pmkt", "abi", "events.json")
    json.dump(reg, open(dst, "w"), indent=1)
    print(f"wrote {len(events)} events -> {dst}")


if __name__ == "__main__":
    main(sys.argv[1])
