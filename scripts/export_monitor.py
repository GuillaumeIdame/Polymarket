"""Export the Polymarket Monitor's data from the lake: one JSON file {tables: {...}, generated_at, meta}.

Every table is computed here from raw/ datasets and data/recon/ outputs, so each figure on the monitor
traces to this code. Run: python scripts/export_monitor.py [out.json]  (default data/monitor/monitor.json)
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import polars as pl

from pmkt import holders, lifecycle, storage
from pmkt.config import load_settings


def _rows(df: pl.DataFrame) -> list[dict]:
    out = []
    for r in df.to_dicts():
        out.append({k: (v.isoformat() if hasattr(v, "isoformat") else
                        (None if isinstance(v, float) and v != v else v)) for k, v in r.items()})
    return out


def main(out_path: str | None = None) -> Path:
    s = load_settings()
    con = storage.connect(s.data_dir)
    q = lambda sql: storage.query(con, sql)  # noqa: E731
    T: dict[str, list[dict]] = {}

    # ---- pipeline health
    spec = [("ws_market", "recv_ts", "Order books & trades (websocket)"),
            ("chain_fills", "block_ts", "Exchange fills (on-chain)"),
            ("chain_transfers", "block_ts", "Token transfers (on-chain)"),
            ("chain_lifecycle", "block_ts", "Split / merge / redeem / migration / resolution"),
            ("api_holders", "snapshot_ts", "Top holders with entry & PnL"),
            ("gamma_markets", "snapshot_ts", "Market metadata (slim)")]
    rows = []
    for ds, col, label in spec:
        try:
            key = {"chain_fills": "tx_hash, log_index", "chain_lifecycle": "tx_hash, log_index",
                   "chain_transfers": "tx_hash, log_index, batch_idx"}.get(ds)
            n = f"count(distinct ({key}))" if key else "count(*)"   # re-indexed ranges overlap: count unique events
            r = q(f"select {n} n, min({col}) a, max({col}) b from {ds}").row(0)
        except Exception:  # noqa: BLE001
            continue
        rows.append({"dataset": ds, "label": label, "rows": r[0], "first": r[1], "last": r[2],
                     "hours": round((r[2] - r[1]).total_seconds() / 3600, 2) if r[1] and r[2] else None})
    gaps = q("""WITH b AS (SELECT DISTINCT block_number FROM chain_fills),
                g AS (SELECT block_number - LAG(block_number) OVER (ORDER BY block_number) d FROM b)
                SELECT count(*) FROM g WHERE d > 30""").item()
    for r in rows:
        r["gaps"] = gaps if r["dataset"] == "chain_fills" else None
    T["pipeline"] = _rows(pl.DataFrame(rows))

    # ---- token -> market map from the latest slim snapshot
    mk = q("""select condition_id, question, category, unnest(asset_ids) token_id, unnest(outcomes) outcome
              from gamma_markets where snapshot_ts = (select max(snapshot_ts) from gamma_markets)""")
    fills = q("select * from chain_fills").unique(subset=["tx_hash", "log_index"])
    f = fills.join(mk.select("token_id", "category"), on="token_id", how="left").with_columns(
        pl.col("category").fill_null(pl.when(pl.col("exchange") == "0xe3333700ca9d93003f00f0f71f8515005f6c00aa")
                                     .then(pl.lit("combos (v2)")).otherwise(pl.lit("unmapped"))))
    tk = f.filter(pl.col("role") == "taker")
    T["flow_by_category"] = _rows(
        tk.group_by("category").agg(taker_fills=pl.len(), notional_usdc=pl.col("usdc").sum(),
                                    taker_fees_usdc=pl.col("fee_usdc").sum(), wallets=pl.col("wallet").n_unique())
        .with_columns(fee_bps=1e4 * pl.col("taker_fees_usdc") / pl.col("notional_usdc"))
        .sort("notional_usdc", descending=True))
    T["flow_5min"] = _rows(
        tk.with_columns(bucket=pl.col("block_ts").dt.truncate("5m"))
        .group_by("bucket").agg(notional_usdc=pl.col("usdc").sum(), taker_fills=pl.len(),
                                taker_fees_usdc=pl.col("fee_usdc").sum()).sort("bucket"))
    T["maker_taker"] = _rows(
        f.group_by("role").agg(fills=pl.len(), wallets=pl.col("wallet").n_unique(), usdc=pl.col("usdc").sum()))

    # ---- holders
    h = q("select * from api_holders where snapshot_ts = (select max(snapshot_ts) from api_holders)")
    if h.height:
        excl = list(s["chain"]["exclude_from_holders"])
        c = holders.concentration(h, exclude=excl).join(
            mk.select("condition_id", "question", "category").unique("condition_id"), on="condition_id", how="left")
        oc = mk.select("token_id", "outcome")
        c = c.join(oc, on="token_id", how="left")
        T["holder_concentration"] = _rows(
            c.filter(pl.col("top_amount") > 5000)
            .select("question", "category", "outcome", "n_holders", "top1_share", "top10_share", "hhi",
                    "avg_entry_w", "n_entry_unknown", "upnl_top", "token_id")
            .sort("top10_share", descending=True))
        top = (h.join(mk.select("token_id", "question", "outcome"), on="token_id", how="left")
               .with_columns(entry_known=pl.col("avg_price") > 0)
               .sort("current_value", descending=True).head(25)
               .select("question", "outcome", "proxy_wallet", "pseudonym", "amount", "avg_price", "current_price",
                       "current_value", "unrealized_pnl", "entry_known"))
        T["top_positions"] = _rows(top)
        T["holders_snapshot"] = _rows(pl.DataFrame([{"snapshot_ts": h["snapshot_ts"][0], "markets": h["condition_id"].n_unique(),
                                                     "rows": h.height}]))

    # ---- lifecycle
    lc = q("select * from chain_lifecycle").unique(subset=["tx_hash", "log_index"])
    T["lifecycle_events"] = _rows(
        lc.group_by("system", "contract_name", "event").agg(events=pl.len(), wallets=pl.col("wallet").n_unique(),
                                                            amount=pl.col("amount").sum(),
                                                            payout=pl.col("payout").sum())
        .sort("events", descending=True))
    cm = lifecycle.condition_map(lc).join(mk.select("condition_id", "category").unique("condition_id"),
                                          left_on="legacy_condition_id", right_on="condition_id", how="left")
    T["migration_registrations"] = _rows(
        cm.with_columns(pl.col("category").fill_null("not in open snapshot"))
        .group_by("category").agg(conditions=pl.len()).sort("conditions", descending=True))

    # ---- V2 checks (C/D/E recomputed here; A/B need receipts and are copied from the recorded run)
    pm = "0x006f54f7f9a22e0000cc2ab60031000000ae9fef"
    ctf = "0x4d97dcd97ec945f40cf65f87097ace5ea0476045"
    pm_ids = q(f"select distinct token_id from chain_transfers where contract='{pm}'")["token_id"].to_list()
    ctf_ids = q(f"select distinct token_id from chain_transfers where contract='{ctf}' limit 50000")["token_id"].to_list()
    v3 = fills.filter(pl.col("exchange") == "0xe3333700ca9d93003f00f0f71f8515005f6c00aa")["token_id"].to_list()
    v1 = fills.filter(pl.col("exchange") != "0xe3333700ca9d93003f00f0f71f8515005f6c00aa")["token_id"].to_list()
    mr = lc.filter(pl.col("event") == "MigrationResolved")["args"].to_list()

    def mr_ok(a):
        a = json.loads(a)
        t = a["legacyPayout0"] + a["legacyPayout1"]
        return t and abs(a["result0"] - 1e6 * a["legacyPayout0"] / t) <= 1 and abs(a["result1"] - 1e6 * a["legacyPayout1"] / t) <= 1
    T["v2_checks"] = [
        {"check": "Migration position ids decode to their condition & outcome", "passed": 115, "total": 115,
         "how": "recorded run, 40k blocks"},
        {"check": "Migrated amount equals the V2 mint in the same transaction", "passed": 60, "total": 60,
         "how": "recorded run, tx receipts"},
        {"check": "V2 ledger token ids recognised as V2", "passed": sum(map(lifecycle.is_v2_position_id, pm_ids)),
         "total": len(pm_ids), "how": "recomputed"},
        {"check": "CTF token ids NOT mistaken for V2", "passed": len(ctf_ids) - sum(map(lifecycle.is_v2_position_id, ctf_ids)),
         "total": len(ctf_ids), "how": "recomputed"},
        {"check": "ExchangeV3 fills use V2 ids", "passed": sum(map(lifecycle.is_v2_position_id, v3)), "total": len(v3),
         "how": "recomputed"},
        {"check": "CTF exchange fills use CTF ids", "passed": len(v1) - sum(map(lifecycle.is_v2_position_id, v1)),
         "total": len(v1), "how": "recomputed"},
        {"check": "Migrated results equal CTF payouts", "passed": sum(bool(mr_ok(a)) for a in mr), "total": len(mr),
         "how": "recomputed"},
    ]

    # ---- reconciliation
    rd = s.data_dir / "recon"
    st = json.loads((rd / "state.json").read_text()) if (rd / "state.json").exists() else {}
    rec = []
    if (rd / "api_t0.parquet").exists():
        a0, b0 = pl.read_parquet(rd / "api_t0.parquet"), pl.read_parquet(rd / "chain_b0.parquet")
        j = a0.join(b0, on=["wallet", "token_id"])
        rec.append({"key": "opening", "stage": "Opening", "check": "API size equals on-chain balance at the start",
                    "matched": j.filter((pl.col("current_size") - pl.col("balance")).abs() < 1e-4).height,
                    "total": j.height})
    summ = json.loads((rd / "summary.json").read_text()) if (rd / "summary.json").exists() else None
    if summ:
        def frac(key):
            m, t = (int(x) for x in str(summ[key]).split("/"))
            return m, t
        for key, rk, stage, label in [
                ("Q_chain_balance_match", "chain", "Quantities", "Opening + ledger equals on-chain balance at close"),
                ("A_v_size", "size", "Data API", "Position size equals the API"),
                ("A_v_avg", "avg", "Data API", "Average entry equals the API (within 0.001)"),
                ("A_v_realized_within_fee", "pnl_fee", "Data API", "Realised PnL equals the API, allowing for fee treatment"),
                ("A_v_realized_exact", "pnl_exact", "Data API", "Realised PnL equals the API exactly (within 1c + 0.1%)"),
                ("A_v_wallets_within_fee", "wallets", "Data API", "Wallet window PnL equals the API (0.5% + fees)")]:
            m, t = frac(key)
            rec.append({"key": rk, "stage": stage, "check": label, "matched": m, "total": t})
        unread_zero = summ.get("Q_unread_zero_opening", 0)
        unread = summ.get("Q_opening_not_read", 0)
        rec.append({"key": "new", "stage": "Quantities", "check": "New positions consistent with a zero opening balance",
                    "matched": unread_zero, "total": unread})
        T["recon_gaps"] = [
            {"key": "archived", "gap": "Positions on archived markets missing from the opening snapshot",
             "pairs": summ.get("A_pairs_opening_gap"), "pnl_effect_usd": summ.get("A_gap_pnl_diff"),
             "cause": "Data API omits archived markets unless include_archived=true", "status": "fixed for next run"},
            {"key": "combos", "gap": "V2 combo positions not listed by /v2/positions",
             "pairs": summ.get("Q_opening_not_read_v2"), "pnl_effect_usd": None,
             "cause": "Combos live on /v2/positions/combos; chain ledger covers them", "status": "excluded from API check"},
            {"key": "ambiguous", "gap": "Redeems where the payout can't say which outcome won",
             "pairs": summ["C_flags"].get("redeem_winner_ambiguous", 0), "pnl_effect_usd": None,
             "cause": "Equal-sized legs burned; wallet total exact, per-outcome split proportional",
             "status": "needs resolution events"},
        ]
        T["recon_pnl"] = [{"key": "verified", "scope": "Verified pairs, all wallets", "ours_usd": summ.get("A_v_pnl_ours"),
                           "api_usd": summ.get("A_v_pnl_api")}]
        T["ledger_mix"] = _rows(pl.DataFrame([{"kind": k, "rows": v} for k, v in summ["C_ledger_rows_by_kind"].items()])
                                .sort("rows", descending=True))
    T["reconciliation"] = rec
    T["recon_window"] = [{"wallets": len(st.get("wallets", [])), "b0": st.get("b0"), "b1": (summ or {}).get("b1"),
                          "status": "complete" if summ else "running"}]

    # ---- screener + smart-money model (scripts/fetch_smartmoney.py, fetch_activity.py, build_screener.py)
    sd = s.data_dir / "smartmoney"
    if (sd / "summary.json").exists():
        summ_sm = json.loads((sd / "summary.json").read_text())
        T["sm_summary"] = [{"key": "model", **{k: v for k, v in summ_sm.items() if not isinstance(v, list)},
                            "validated": ", ".join(summ_sm.get("validated") or []) or "none"}]
        T["sm_definitions"] = _rows(pl.read_parquet(sd / "definitions.parquet"))
        T["sm_calibration"] = _rows(pl.read_parquet(sd / "calibration.parquet"))
        scr = pl.read_parquet(sd / "screener.parquet")
        scr = scr.with_columns(
            screen=pl.concat_str([
                pl.when(pl.col("near_res")).then(pl.lit("Near resolution")).otherwise(pl.lit("")),
                pl.when(pl.col("smart_lean").is_not_null() & (pl.col("smart_lean") != "split"))
                .then(pl.lit("Top-scored wallets lean ") + pl.col("smart_lean")).otherwise(pl.lit("")),
                pl.when(pl.col("insider_flags") > 0).then(pl.lit("Fresh-wallet size")).otherwise(pl.lit("")),
            ], separator=" · ").str.strip_chars(" ·").str.replace_all(r"( · )+", " · "))
        T["screener"] = _rows(scr.filter(pl.col("hours_left") > 0).sort("volume_24h", descending=True, nulls_last=True)
                              .head(400).drop("condition_id"))
        ws = pl.read_parquet(sd / "wallet_scores.parquet")
        prim = summ_sm["primary"]
        T["sm_wallets"] = _rows(ws.filter(pl.col("smart")).sort(prim, descending=True).head(25)
                                .select("wallet", "events", "capital", "pnl", "roi", "roi_ex_best", prim,
                                        "calibration", "is_mm").rename({prim: "score"}))
        if (sd / "insider.parquet").exists():
            ins = pl.read_parquet(sd / "insider.parquet")
            if ins.height:
                T["insider"] = _rows(ins.sort("current_value", descending=True).head(30)
                                     .select("question", "proxy_wallet", "pseudonym", "outcome_index", "amount",
                                             "avg_price", "current_value", "age_days", "trades"))
    # ---- playbook (knowledge base) claims tested: scripts/test_kb_hypotheses.py
    if (sd / "kb_hypotheses.parquet").exists():
        kh = pl.read_parquet(sd / "kb_hypotheses.parquet")
        keep = [c for c in ["hypothesis", "wallets", "test_events", "spearman", "p_value", "top_q_edge", "top_q_net",
                            "bottom_q_edge", "note"] if c in kh.columns]
        T["kb_tests"] = _rows(kh.select(keep).with_columns(key=pl.col("hypothesis").str.extract(r"^(\S+)", 1)))
        sims = pl.read_parquet(sd / "kb_copy_sims.parquet")
        T["kb_sims"] = _rows(sims.group_by("selection").agg(
            wallets=pl.len(), median_final=pl.col("final").median(), p25=pl.col("final").quantile(0.25),
            p75=pl.col("final").quantile(0.75), mean_final=pl.col("final").mean(),
            mean_ex_best=pl.col("final").sort().head(pl.len() - 1).mean(), best=pl.col("final").max(),
            share_profitable=(pl.col("ret") > 0).mean(), median_drawdown=pl.col("max_drawdown").median())
            .sort("median_final", descending=True))
        T["kb_categories"] = _rows(pl.read_parquet(sd / "kb_categories.parquet"))
    out = Path(out_path or s.data_dir / "monitor" / "monitor.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {"generated_at": datetime.now(timezone.utc).isoformat(), "tables": T,
               "meta": [{"label": "export_monitor.py", "value": Path(__file__).read_text(), "type": "code",
                         "lang": "python"},
                        {"label": "Input", "value": "pmkt Parquet lake (raw/*) + data/recon/* in the research container"}]}
    out.write_text(json.dumps(payload, default=str))
    print(f"wrote {out} ({out.stat().st_size // 1024} KB): " + ", ".join(f"{k}={len(v)}" for k, v in T.items()))
    return out


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
