"""Event-sourced wallet ledger: every share movement, priced by its on-chain context.

Quantities come from ERC-1155 transfers (canonical, docs: onchain-position-data). For each
(tx, wallet, token) the transfer delta is explained, in order, by:
  1. exchange fills (OrderFilled): exact price and fee           -> BUY / SELL
  2. lifecycle events in the same tx that mention the wallet:
       PositionMigrated (V2)                                      -> MIGRATE_OUT / MIGRATE_IN (basis carried)
       PayoutRedemption / PositionRedeemed / *Redemption          -> REDEEM (cash = payout)
       PositionsMerge / PositionsMerged                           -> MERGE  (cash = collateral)
       PositionSplit / PositionsSplit                             -> SPLIT  (cost = collateral)
       PositionsConverted (neg-risk)                              -> CONVERT_OUT / CONVERT_IN (basis carried, net of collateral)
  3. anything else: wallet<->wallet                               -> TRANSFER_OUT / TRANSFER_IN

Collateral of a split/merge is allocated across the outcomes it created/destroyed using a reference
price per token (last fill in window, else equal split). The total P&L of a split/merge is invariant to
that choice once every leg is closed; only per-outcome attribution depends on it.
TRANSFER_IN from an untracked sender carries an unknown basis: booked at the reference price and flagged.

Accounting = average cost incl. fees (matches Data API v2 `avg_price`, which includes entry fees).
"""
from __future__ import annotations

import json
from collections import defaultdict

import polars as pl

ZERO = "0x" + "0" * 40
EPS = 1e-6

REDEEM_EVENTS = {"PayoutRedemption", "PositionRedeemed", "BinaryRedemption", "NegRiskRedemption", "Redemption"}
MERGE_EVENTS = {"PositionsMerge", "PositionsMerged"}
SPLIT_EVENTS = {"PositionSplit", "PositionsSplit"}
CONVERT_EVENTS = {"PositionsConverted", "PositionConverted"}
MIGRATE_EVENTS = {"PositionMigrated"}

LEDGER_SCHEMA = {
    "wallet": pl.Utf8, "token_id": pl.Utf8, "block_number": pl.Int64, "tx_hash": pl.Utf8, "ord": pl.Int64,
    "kind": pl.Utf8, "delta": pl.Float64, "cash": pl.Float64, "fee": pl.Float64, "carry_key": pl.Utf8,
    "counterparty": pl.Utf8, "flag": pl.Utf8,
}


def _wallet_events(lc: pl.DataFrame, wallets: set[str]) -> dict[tuple[str, str], list[dict]]:
    """(tx, wallet) -> lifecycle rows whose decoded args mention the wallet (initiator/recipient/redeemer...)."""
    out: dict[tuple[str, str], list[dict]] = defaultdict(list)
    if lc.height == 0:
        return out
    for r in lc.iter_rows(named=True):
        args = (r["args"] or "").lower()
        hit = {w for w in wallets if w in args} | ({r["wallet"]} & wallets)
        for w in hit:
            out[(r["tx_hash"], w)].append(r)
    return out


def _alloc(tokens: list[str], shares: dict[str, float], total: float, ref: dict[str, float]) -> dict[str, float]:
    """Split `total` collateral across tokens in proportion to shares x reference price.
    Missing prices are filled as the complement (1 - sum of known) / n_missing; equal split if nothing usable."""
    known = {t: ref[t] for t in tokens if ref.get(t) is not None}
    miss = [t for t in tokens if t not in known]
    if miss and known:
        fill = max(1.0 - sum(known.values()), 0.0) / len(miss)
        known.update({t: fill for t in miss})
    w = {t: shares[t] * known.get(t, 1.0) for t in tokens}
    s = sum(w.values())
    if s <= EPS:
        w = {t: shares[t] for t in tokens}
        s = sum(w.values()) or 1.0
    return {t: total * w[t] / s for t in tokens}


class RefPrice:
    """Point-in-time reference price: last fill strictly before a block (no look-ahead)."""

    def __init__(self, fills: pl.DataFrame):
        self.px: dict[str, tuple[list[int], list[float]]] = {}
        for tok, g in fills.sort("block_number", "log_index").group_by("token_id", maintain_order=True):
            self.px[tok[0]] = (g["block_number"].to_list(), g["price"].to_list())

    def at(self, tok: str, block: int | None) -> float | None:
        import bisect
        if tok not in self.px or block is None:
            return None
        blks, prices = self.px[tok]
        i = bisect.bisect_left(blks, block) - 1
        return prices[i] if i >= 0 else None


def build_ledger(fills: pl.DataFrame, transfers: pl.DataFrame, lifecycle: pl.DataFrame, wallets: set[str],
                 protocol: list[str], winners: set[str] | None = None) -> pl.DataFrame:
    wallets = {w.lower() for w in wallets}
    prot = {p.lower() for p in protocol} | {ZERO}
    fills = fills.unique(subset=["tx_hash", "log_index"])
    transfers = transfers.unique(subset=["tx_hash", "log_index", "batch_idx"])
    lc = lifecycle.unique(subset=["tx_hash", "log_index"]) if lifecycle.height else lifecycle

    rp = RefPrice(fills)

    # 1) transfer deltas per (tx, wallet, token) and counterparties
    tdelta: dict[tuple, float] = defaultdict(float)
    tcp: dict[tuple, set] = defaultdict(set)
    tmeta: dict[str, tuple[int, int]] = {}
    for tx, blk, li, frm, to, tok, amt in transfers.select(
            "tx_hash", "block_number", "log_index", "from_addr", "to_addr", "token_id", "amount").iter_rows():
        tmeta[tx] = min(tmeta.get(tx, (blk, li)), (blk, li))
        if to in wallets:
            tdelta[(tx, to, tok)] += amt
            tcp[(tx, to, tok)].add(frm)
        if frm in wallets:
            tdelta[(tx, frm, tok)] -= amt
            tcp[(tx, frm, tok)].add(to)

    rows: list[dict] = []

    def add(tx, w, tok, kind, delta, cash=0.0, fee=0.0, carry=None, cp=None, flag=None):
        blk, li = tmeta.get(tx, (None, None))
        rows.append({"wallet": w, "token_id": tok, "block_number": blk, "tx_hash": tx, "ord": li, "kind": kind,
                     "delta": delta, "cash": cash, "fee": fee, "carry_key": carry, "counterparty": cp, "flag": flag})

    # 2) fills
    fdelta: dict[tuple, float] = defaultdict(float)
    for tx, blk, li, w, side, tok, q, usdc, fee in fills.filter(pl.col("wallet").is_in(list(wallets))).select(
            "tx_hash", "block_number", "log_index", "wallet", "side", "token_id", "shares", "usdc",
            "fee_usdc").iter_rows():
        tmeta.setdefault(tx, (blk, li))
        d = q if side == "BUY" else -q
        fdelta[(tx, w, tok)] += d
        add(tx, w, tok, side, d, cash=(-usdc if side == "BUY" else usdc) - fee, fee=fee)

    # 3) residuals -> lifecycle classification
    evs = _wallet_events(lc, wallets)
    resid: dict[tuple[str, str], dict[str, float]] = defaultdict(dict)
    for key in set(tdelta) | set(fdelta):
        r = tdelta.get(key, 0.0) - fdelta.get(key, 0.0)
        if abs(r) > EPS:
            tx, w, tok = key
            resid[(tx, w)][tok] = r
            if key in fdelta and key not in tdelta:
                pass  # fill without a transfer for this wallet: shows up as a negative/positive residual

    tx_names: dict[str, set[str]] = defaultdict(set)
    for tx, ev in (lc.select("tx_hash", "event").iter_rows() if lc.height else []):
        tx_names[tx].add(ev)

    # Intermediaries are discovered, not listed: anything acting in the tx as a lifecycle actor
    # (stakeholder/redeemer/initiator) or as an ERC-1155 operator, plus known protocol addresses + zero.
    # This catches undocumented collateral adapters (e.g. 0xada100874d00..., 0xada200001000...).
    actors: dict[str, set[str]] = defaultdict(set)
    for tx, op in transfers.select("tx_hash", "operator").iter_rows():
        actors[tx].add(op)
    for tx, a in (lc.select("tx_hash", "wallet").iter_rows() if lc.height else []):
        if a:
            actors[tx].add(a)

    def is_prot(cp: str, tx: str) -> bool:
        cs = [c for c in cp.split(",") if c]
        return bool(cs) and all(c in prot or (c in actors[tx] and c not in wallets) for c in cs)

    for (tx, w), toks in resid.items():
        ev_rows = evs.get((tx, w), [])
        names = {e["event"] for e in ev_rows}
        txn = tx_names.get(tx, set())
        ins = {t: d for t, d in toks.items() if d > 0}
        outs = {t: -d for t, d in toks.items() if d < 0}
        cps = {t: ",".join(sorted(tcp.get((tx, w, t), set()))) for t in toks}
        blk = tmeta.get(tx, (None, None))[0]
        ref = {t: rp.at(t, blk) for t in toks}
        if names & MIGRATE_EVENTS:
            for t, q in outs.items():
                add(tx, w, t, "MIGRATE_OUT", -q, carry=f"mig:{w}:{round(q, 6)}", cp=cps[t])
            for t, q in ins.items():
                add(tx, w, t, "MIGRATE_IN", q, carry=f"mig:{w}:{round(q, 6)}", cp=cps[t])
            continue
        if names & CONVERT_EVENTS:
            e = next(x for x in ev_rows if x["event"] in CONVERT_EVENTS)
            a = json.loads(e["args"])
            k = bin(int(a.get("indexSet", 0))).count("1")
            coll = (a.get("amount", 0) / 1e6) * max(k - 1, 0)
            for t, q in outs.items():
                add(tx, w, t, "CONVERT_OUT", -q, carry=f"conv:{w}", cp=cps[t])
            for t, q in ins.items():
                add(tx, w, t, "CONVERT_IN", q, cash=0.0, carry=f"conv:{w}", cp=cps[t])
            if coll:
                add(tx, w, "__collateral__", "CONVERT_CASH", 0.0, cash=coll, carry=f"conv:{w}")
            continue
        # Per-share economics (robust to adapters/batches where the event names an intermediary):
        #   redeem: winner pays 1/share, loser 0 ; merge: 1 collateral per complete set ; split: costs 1 per set.
        prot_outs = {t: q for t, q in outs.items() if is_prot(cps[t], tx)}
        prot_ins = {t: q for t, q in ins.items() if is_prot(cps[t], tx)}
        if prot_outs and (names | txn) & REDEEM_EVENTS:
            own = sum((x["payout"] or 0.0) for x in ev_rows if x["event"] in REDEEM_EVENTS)
            for t, q in prot_outs.items():
                if winners is not None and (t in winners or own == 0):
                    cash, flag = (q if t in winners else 0.0), None
                elif len(prot_outs) == 1 and own:
                    cash, flag = own, None
                else:
                    cash, flag = q * round(ref.get(t) or 0.0), "redeem_fraction_from_price"
                add(tx, w, t, "REDEEM", -q, cash=cash, cp=cps[t], flag=flag)
                outs.pop(t)
        if len(prot_outs) >= 1 and outs and (names | txn) & MERGE_EVENTS:
            mo = {t: q for t, q in outs.items() if t in prot_outs}
            sets = min(mo.values()) if len(mo) >= 2 else None
            alloc = _alloc(list(mo), mo, sets, ref) if sets else {t: q * (ref.get(t) or 0.5) for t, q in mo.items()}
            for t, q in mo.items():
                add(tx, w, t, "MERGE", -q, cash=alloc[t], cp=cps[t], flag=None if sets else "partial_merge")
                outs.pop(t)
        if prot_ins and (names | txn) & SPLIT_EVENTS:
            sets = min(prot_ins.values()) if len(prot_ins) >= 2 else None
            alloc = _alloc(list(prot_ins), prot_ins, sets, ref) if sets else \
                {t: q * (ref.get(t) or 0.5) for t, q in prot_ins.items()}
            for t, q in prot_ins.items():
                add(tx, w, t, "SPLIT", q, cash=-alloc[t], cp=cps[t], flag=None if sets else "partial_split")
                ins.pop(t)
        for t, q in outs.items():
            add(tx, w, t, "TRANSFER_OUT", -q, carry=f"xfer:{t}", cp=cps[t])
        for t, q in ins.items():
            prot_only = is_prot(cps[t], tx)
            add(tx, w, t, "TRANSFER_IN", q, cash=-q * (ref.get(t) or 0.0), carry=f"xfer:{t}", cp=cps[t],
                flag="unexplained_protocol_inflow" if prot_only else "basis_unknown")
    return pl.DataFrame(rows, schema=LEDGER_SCHEMA)


class Book:
    """Average-cost book with basis carry. Seed with an opening book, then apply ledger events."""

    def __init__(self):
        self.st = defaultdict(lambda: {"pos": 0.0, "cost": 0.0, "realized": 0.0, "fees": 0.0})

    def seed(self, wallet, token, size, avg, realized=0.0):
        s = self.st[(wallet, token)]
        s.update(pos=size, cost=size * avg, realized=realized)

    def _remove(self, s, q):
        avg = s["cost"] / s["pos"] if s["pos"] > EPS else 0.0
        q_cov = min(q, max(s["pos"], 0.0))
        basis = q_cov * avg
        s["pos"] -= q_cov
        s["cost"] -= basis
        if s["pos"] < EPS:
            s["pos"], s["cost"] = 0.0, 0.0
        return basis, q - q_cov

    def apply_tx(self, events: list[dict], ref: dict[str, float]) -> list[str]:
        """Outs first (they create carried basis), then ins. Returns flags raised."""
        flags, pool, pool_q = [], defaultdict(float), defaultdict(float)
        conv = [e for e in events if e["kind"] == "CONVERT_CASH"]
        for e in sorted(events, key=lambda e: e["delta"]):           # negatives first
            if e["kind"] == "CONVERT_CASH" or e["delta"] >= 0:
                continue
            s = self.st[(e["wallet"], e["token_id"])]
            basis, uncovered = self._remove(s, -e["delta"])
            if uncovered > EPS:
                flags.append(f"oversold:{e['kind']}")
            if e["kind"] in ("SELL", "MERGE", "REDEEM"):
                s["realized"] += e["cash"] - basis      # cash is net of fee
                s["fees"] += e["fee"]
            else:
                pool[e["carry_key"]] += basis
                pool_q[e["carry_key"]] += -e["delta"]
        # convert: collateral received is realised against the NO basis given up, remainder carried
        for c in conv:
            b = pool.get(c["carry_key"], 0.0)
            used = min(c["cash"], b)
            first = next(e for e in events if e["kind"] == "CONVERT_OUT" and e["wallet"] == c["wallet"])
            self.st[(first["wallet"], first["token_id"])]["realized"] += c["cash"] - used
            pool[c["carry_key"]] = b - used
        ins = [e for e in events if e["delta"] > 0 and e["kind"] != "CONVERT_CASH"]
        in_q = defaultdict(float)
        for e in ins:
            in_q[e["carry_key"]] += e["delta"]
        for e in ins:
            s = self.st[(e["wallet"], e["token_id"])]
            if e["kind"] in ("BUY", "SPLIT"):
                cost = -e["cash"]
                s["fees"] += e["fee"]
            elif e["carry_key"] in pool and pool_q[e["carry_key"]] > EPS:
                cost = pool[e["carry_key"]] * e["delta"] / in_q[e["carry_key"]]
            else:  # no carried basis: point-in-time reference price stored in cash by build_ledger
                cost = -e["cash"] if e["cash"] else e["delta"] * ref.get(e["token_id"], 0.0)
                flags.append(f"basis_unknown:{e['kind']}")
            s["pos"] += e["delta"]
            s["cost"] += cost
        return flags

    def frame(self) -> pl.DataFrame:
        return pl.DataFrame([{"wallet": w, "token_id": t, "position": s["pos"], "cost_basis": s["cost"],
                              "avg_entry": s["cost"] / s["pos"] if s["pos"] > EPS else None,
                              "realized_pnl": s["realized"], "fees": s["fees"]}
                             for (w, t), s in self.st.items()],
                            schema={"wallet": pl.Utf8, "token_id": pl.Utf8, "position": pl.Float64,
                                    "cost_basis": pl.Float64, "avg_entry": pl.Float64,
                                    "realized_pnl": pl.Float64, "fees": pl.Float64})


def replay(ledger: pl.DataFrame, book: Book | None = None, ref: dict[str, float] | None = None) -> tuple[Book, list]:
    book = book or Book()
    ref = ref or {}
    flags = []
    led = ledger.sort(["block_number", "ord", "tx_hash"], nulls_last=True)
    for _, grp in led.group_by("tx_hash", maintain_order=True):   # whole tx: basis can move between wallets
        flags += book.apply_tx(grp.to_dicts(), ref)
    return book, flags


def reconcile_window(led: pl.DataFrame, api0: pl.DataFrame, api1: pl.DataFrame, bal0: pl.DataFrame,
                     bal1: pl.DataFrame, b0: int, fetch: dict[str, dict], tol_size: float = 1e-3) -> dict:
    """Three checks:
      Q  chain quantities : balanceOf(B0) + sum(ledger deltas in (B0,B1]) == balanceOf(B1)
      C  classification   : share of ledger rows by kind, flags
      A  Data API         : seed each wallet with API@T0 (taken in [start_w,end_w]), replay (end_w, B1],
                            compare size / avg_price / realized_pnl with API@T1. Pairs touched inside the
                            fetch bracket are ambiguous and excluded.
    """
    led = led.filter(pl.col("kind") != "CONVERT_CASH")
    # Q
    d = led.filter(pl.col("block_number") > b0).group_by("wallet", "token_id").agg(delta=pl.col("delta").sum())
    q = (bal1.join(bal0.rename({"balance": "b0"}), on=["wallet", "token_id"], how="full", coalesce=True)
         .join(d, on=["wallet", "token_id"], how="left")
         .with_columns(pl.col("b0", "balance", "delta").fill_null(0.0))
         .with_columns(err=pl.col("b0") + pl.col("delta") - pl.col("balance")))
    touched = q.filter(pl.col("delta").abs() > EPS)
    q_ok = touched.filter(pl.col("err").abs() <= tol_size)

    # A
    ref = {}
    ambiguous = set()
    keep = []
    for w, br in fetch.items():
        lw = led.filter(pl.col("wallet") == w)
        amb = lw.filter((pl.col("block_number") >= br["start"]) & (pl.col("block_number") <= br["end"]))
        ambiguous |= {(w, t) for t in amb["token_id"].to_list()}
        keep.append(lw.filter(pl.col("block_number") > br["end"]))
    led_a = pl.concat(keep) if keep else led.clear()
    book = Book()
    for r in api0.iter_rows(named=True):
        book.seed(r["wallet"], r["token_id"], r["current_size"] or 0.0, r["avg_price"] or 0.0,
                  r["realized_pnl"] or 0.0)
    book, flags = replay(led_a, book, ref)
    ours = book.frame()
    a1 = api1.select("wallet", "token_id", api_size="current_size", api_avg="avg_price",
                     api_realized="realized_pnl", api_status="status")
    act = led_a.group_by("wallet", "token_id").agg(kinds=pl.col("kind").unique().sort().str.join("+"),
                                                    n_events=pl.len())
    cmp = (act.join(ours, on=["wallet", "token_id"], how="left")
           .join(a1, on=["wallet", "token_id"], how="left")
           .filter(~pl.struct("wallet", "token_id").map_elements(
               lambda s: (s["wallet"], s["token_id"]) in ambiguous, return_dtype=pl.Boolean))
           .with_columns(pl.col("api_size", "api_realized").fill_null(0.0))
           .with_columns(size_ok=(pl.col("position") - pl.col("api_size")).abs() <= tol_size,
                         avg_ok=(pl.col("position") < tol_size) | pl.col("api_avg").is_null()
                         | ((pl.col("avg_entry") - pl.col("api_avg")).abs() <= 1e-3),
                         realized_ok=(pl.col("realized_pnl") - pl.col("api_realized")).abs()
                         <= 0.01 + 1e-3 * pl.col("api_realized").abs()))
    by_kind = (cmp.group_by("kinds").agg(n=pl.len(), size_ok=pl.col("size_ok").mean(),
                                         avg_ok=pl.col("avg_ok").mean(), realized_ok=pl.col("realized_ok").mean())
               .sort("n", descending=True))
    summary = {
        "Q_pairs_with_activity": touched.height,
        "Q_chain_balance_match": f"{q_ok.height}/{touched.height}",
        "C_ledger_rows_by_kind": dict(led.group_by("kind").len().iter_rows()),
        "C_flags": dict(led.filter(pl.col("flag").is_not_null()).group_by("flag").len().iter_rows()),
        "A_pairs_compared": cmp.height,
        "A_ambiguous_excluded": len(ambiguous),
        "A_size_match": f"{cmp['size_ok'].sum()}/{cmp.height}",
        "A_avg_match": f"{cmp['avg_ok'].sum()}/{cmp.height}",
        "A_realized_match": f"{cmp['realized_ok'].sum()}/{cmp.height}",
        "A_replay_flags": {f: flags.count(f) for f in set(flags)},
    }
    return {"summary": summary, "frames": {"quantity": q, "api": cmp, "by_kind": by_kind}}
