"""Test the claims of an external Polymarket knowledge base (influencer playbook) against our trade data.

Each claim is turned into a falsifiable hypothesis and tested walk-forward on the unbiased trade sample
(/v2/activity + /v2/resolutions; scripts/fetch_activity.py). Same cutoff for every test, same copy costs.

H1 specialists (>=70% of events in one category) persist and are copyable when copying only their specialty
H2 esports specialists (CS2 / LoL / Dota 2 / Valorant) in particular
H3 selecting wallets on headline 30-day stats (PnL, win rate, ROI) - the case-study method - picks future winners
H4 a $400 copy portfolio of the selected wallets grows after fees, spread, latency and capital lock-up
H5 fresh wallets betting big at low prices win more often than the price implies (insider signature)
H6 win rate predicts future edge

Output: data/smartmoney/kb_{hypotheses,copy_sims,categories}.parquet and kb_summary.json
"""
from __future__ import annotations

import heapq
import json
import sys
from datetime import datetime, timedelta

import numpy as np
import polars as pl

from pmkt import smartmoney as sm
from pmkt.config import load_settings

ESPORTS = {"cs2", "lol", "dota2", "val", "worlds", "r6", "r6siege", "mlbb", "codm", "ow", "rl", "sc2"}
CRYPTO = {"btc", "eth", "sol", "xrp", "bnb", "doge", "hype", "zec", "bitcoin", "ethereum", "solana"}
SPORTS = {"atp", "wta", "itf", "nba", "nfl", "cfb", "nhl", "mlb", "epl", "ucl", "uel", "uefa", "lal", "sea", "bun",
          "fl1", "mls", "ufc", "fif", "bra", "arg", "mex", "col", "uwcl", "cbb", "ncaa", "unl", "pro", "ere", "por",
          "tur", "aus", "jpn", "kor", "chn", "box", "f1", "golf", "pga", "cric", "ipl", "wnba", "ballon"}
FEE = {"esports": 0.05, "sports": 0.05, "crypto_short": 0.07, "crypto": 0.07, "weather": 0.05, "mentions": 0.04,
       "macro": 0.05, "politics": 0.04, "geopolitics": 0.0, "narrative": 0.04, "other": 0.05}


def categorise(pos: pl.DataFrame) -> pl.DataFrame:
    slug = pl.col("event_id").str.to_lowercase()
    title = pl.col("title").fill_null("").str.to_lowercase()
    pre = slug.str.extract(r"^([a-z0-9]+)-", 1)
    return pos.with_columns(category=(
        pl.when(pre.is_in(list(ESPORTS)) | title.str.contains(r"counter-strike|league of legends|dota 2|valorant|rainbow six|\(bo[135]\)|esports")).then(pl.lit("esports"))
        .when(pre.is_in(list(CRYPTO)) & (slug.str.contains("updown") | slug.str.contains("up-or-down"))).then(pl.lit("crypto_short"))
        .when(pre.is_in(list(CRYPTO)) | title.str.contains("bitcoin|ethereum|solana|crypto")).then(pl.lit("crypto"))
        .when(pre.is_in(["highest", "lowest"]) | title.str.contains("temperature")).then(pl.lit("weather"))
        .when(pre.is_in(list(SPORTS)) | title.str.contains(r" vs\.? |o/u \d|spread|win on 20\d\d|both teams|halftime|exact score|moneyline")).then(pl.lit("sports"))
        .when(pre.is_in(["elon"]) | title.str.contains("tweet|post .* times|say ")).then(pl.lit("mentions"))
        .when(pre.is_in(["fed"]) | title.str.contains("fed |interest rate|cpi|inflation|unemployment|gdp|recession")).then(pl.lit("macro"))
        .when(title.str.contains("ceasefire|israel|iran|ukraine|russia|china|taiwan|strike|war|netanyahu|hamas|gaza")).then(pl.lit("geopolitics"))
        .when(title.str.contains("election|president|senate|house|trump|governor|nominee|mayor|parliament|minister|vote")).then(pl.lit("politics"))
        .when(title.str.contains("jensen|ceo|largest company|ipo|openai|apple|nvidia|tesla|resign")).then(pl.lit("narrative"))
        .otherwise(pl.lit("other"))))


def copy_sim(trades: pl.DataFrame, capital: float = 400.0, frac: float = 0.05, slip: float = 0.02,
             max_stake: float = 50.0) -> dict:
    """Follow every trade of the selected wallets in time order. Stake = frac x free cash (<= max_stake, >= $1).
    Entry price = their price + slip (spread + 1-3 min latency); taker fee by category; cash locked until the
    market resolves. Returns final equity and drawdown."""
    # unknown resolution time (Gamma-backfilled payouts): settle at the end of the window - capital stays locked
    far = trades["t_entry"].max()
    t = trades.sort("t_entry").with_columns(pl.col("t_out").fill_null(far)).select("t_entry", "t_out", "p", "x", "category").rows()
    cash, open_q, peak, mdd, n = capital, [], capital, 0.0, 0
    locked = 0.0
    for t_in, t_out, p, x, cat in t:
        while open_q and open_q[0][0] <= t_in:        # settle everything resolved before this entry
            _, pay, stake = heapq.heappop(open_q)
            cash += pay
            locked -= stake
        pe = min(0.99, p + slip)
        stake = min(max_stake, frac * cash)
        if stake < 1.0:
            continue
        shares = stake / pe
        fee = shares * FEE.get(cat, 0.05) * pe * (1 - pe)
        if stake + fee > cash:
            continue
        cash -= stake + fee
        locked += stake
        heapq.heappush(open_q, (t_out, shares * x, stake))
        n += 1
        eq = cash + locked
        peak = max(peak, eq)
        mdd = max(mdd, (peak - eq) / peak)
    while open_q:
        _, pay, stake = heapq.heappop(open_q)
        cash += pay
    return {"final": cash, "ret": cash / capital - 1, "trades_copied": n, "max_drawdown": mdd}


def head_to_head(name: str, pos_train_scope: pl.DataFrame, pos_test_scope: pl.DataFrame, cutoff: datetime) -> dict:
    """Walk-forward PnL-skill test on a scope (train and test scopes may differ, e.g. specialty-only copying)."""
    both = pl.concat([pos_train_scope.filter(pl.col("t_entry") < cutoff),
                      pos_test_scope.filter(pl.col("t_entry") >= cutoff)], how="vertical_relaxed")
    res, panel = sm.walk_forward(both, cutoff, n_perm=1000)
    r = res.filter(pl.col("definition") == "pnl_skill").to_dicts()
    r = r[0] if r else {"definition": "pnl_skill"}
    return {"hypothesis": name, **{k: sm.safe(v) for k, v in r.items()}}, panel


def main(cutoff_days: int = 21):
    s = load_settings()
    d = s.data_dir / "smartmoney"
    now = datetime.fromisoformat(json.loads((d / "activity_meta.json").read_text())["fetched_at"])
    cutoff = now - timedelta(days=cutoff_days)
    pos = categorise(sm.trades_to_positions(pl.read_parquet(d / "trades.parquet"), pl.read_parquet(d / "resolutions.parquet")))
    pos.group_by("category").agg(trades=pl.len(), wallets=pl.col("wallet").n_unique(), events=pl.col("event_id").n_unique(),
                                 excess=(pl.col("e") * pl.col("cap")).sum() / pl.col("cap").sum()) \
        .sort("trades", descending=True).write_parquet(d / "kb_categories.parquet")
    train = pos.filter((pl.col("t_out") < cutoff) & (pl.col("t_entry") < cutoff))
    test = pos.filter(pl.col("t_entry") >= cutoff)

    # specialty per wallet, from train only
    ev = train.group_by("wallet", "category").agg(ev=pl.col("event_id").n_unique())
    tot = ev.group_by("wallet").agg(tot=pl.col("ev").sum())
    top = ev.sort("ev", descending=True).group_by("wallet").first().join(tot, on="wallet") \
        .with_columns(share=pl.col("ev") / pl.col("tot"))
    spec = top.filter((pl.col("share") >= 0.7) & (pl.col("ev") >= 20)).select("wallet", spec_cat="category")
    gen = top.filter(pl.col("share") < 0.7).select("wallet")
    print(f"specialists {spec.height}, generalists {gen.height}; specialties: "
          f"{dict(spec.group_by('spec_cat').len().iter_rows())}", flush=True)

    rows, panels = [], {}
    base, _ = head_to_head("Baseline: all wallets, all trades", pos, pos, cutoff)
    rows.append(base)
    sp_pos = pos.join(spec, on="wallet").filter(pl.col("category") == pl.col("spec_cat")).drop("spec_cat")
    r, panels["H1"] = head_to_head("H1 Specialists, copying only their specialty", sp_pos, sp_pos, cutoff)
    rows.append(r)
    r, _ = head_to_head("H1b Generalists, all trades", pos.join(gen, on="wallet"), pos.join(gen, on="wallet"), cutoff)
    rows.append(r)
    es = sp_pos.join(spec.filter(pl.col("spec_cat") == "esports").select("wallet"), on="wallet")
    r, panels["H2"] = head_to_head("H2 Esports specialists (CS2/LoL/Dota/Valorant)", es, es, cutoff)
    rows.append(r)

    # H3: case-study selection on the last 30 days before the cutoff: PnL rank, win rate >= 60%, ROI > 0
    w30 = train.filter(pl.col("t_entry") >= cutoff - timedelta(days=30)) \
        .group_by("wallet").agg(pnl=pl.col("pnl").sum(), cap=pl.col("cap").sum(), win=(pl.col("x") > pl.col("p")).mean(),
                                events=pl.col("event_id").n_unique()) \
        .with_columns(roi=pl.col("pnl") / pl.col("cap")).filter(pl.col("events") >= 10)
    picked = w30.filter((pl.col("win") >= 0.6) & (pl.col("roi") > 0)).sort("pnl", descending=True).head(25)
    # H6: does win rate predict test edge?
    te = test.group_by("wallet").agg(test_edge=(pl.col("e") * pl.col("cap")).sum() / pl.col("cap").sum(),
                                     test_events=pl.col("event_id").n_unique())
    wr = w30.join(te, on="wallet").filter(pl.col("test_events") >= 5)
    rho_wr = float(np.corrcoef(wr["win"].rank().to_numpy(), wr["test_edge"].rank().to_numpy())[0, 1]) if wr.height > 30 else None

    # H4: $400 copy simulations (test period only)
    def sims(label, wallets, scope):
        out = []
        for w in wallets:
            tr = scope.filter((pl.col("wallet") == w) & (pl.col("t_entry") >= cutoff))
            if tr.height:
                out.append({"selection": label, "wallet": w, **copy_sim(tr)})
        return out
    q = lambda p, d: p.filter(pl.col("pnl_skill") >= p["pnl_skill"].quantile(0.8))["wallet"].to_list() if p.height else []  # noqa: E731
    _, base_panel = sm.walk_forward(pos, cutoff, n_perm=10)
    sim_rows = (sims("H3 Case-study picks (top 30-day PnL, win rate >= 60%)", picked["wallet"].to_list(), pos)
                + sims("Top fifth by PnL skill, all trades", q(base_panel, None), pos)
                + sims("H1 Top-fifth specialists, specialty only", q(panels["H1"], None), sp_pos)
                + sims("H2 Esports specialists, specialty only", panels["H2"]["wallet"].to_list() if panels["H2"].height else [], es))
    sim = pl.DataFrame(sim_rows, infer_schema_length=None)
    sim.write_parquet(d / "kb_copy_sims.parquet")
    sim_sum = sim.group_by("selection").agg(wallets=pl.len(), median_final=pl.col("final").median(),
                                            mean_final=pl.col("final").mean(), share_profitable=(pl.col("ret") > 0).mean(),
                                            best=pl.col("final").max(), worst=pl.col("final").min(),
                                            median_drawdown=pl.col("max_drawdown").median())
    print(sim_sum)

    # H5: fresh wallets, big low-price bets
    st = pl.read_parquet(d / "user_stats.parquet").select("wallet", "join_date")
    fz = pos.join(st, on="wallet", how="inner").with_columns(
        age_d=(pl.col("t_entry").dt.epoch("s") - pl.col("join_date").cast(pl.Int64)) / 86400)
    fresh = fz.filter((pl.col("age_d") <= 7) & (pl.col("p") <= 0.25) & (pl.col("cap") >= 500))
    old = fz.filter((pl.col("age_d") > 90) & (pl.col("p") <= 0.25) & (pl.col("cap") >= 500))

    def exc(df):
        if df.height == 0:
            return None, 0, 0
        return float((df["x"] - df["p"]).mean()), df.height, df["event_id"].n_unique()
    f_ex, f_n, f_ev = exc(fresh)
    o_ex, o_n, o_ev = exc(old)
    rows.append({"hypothesis": "H5 Fresh wallets (<=7d), stake >= $500 at <= 25c: excess win vs price",
                 "top_q_edge": f_ex, "bottom_q_edge": o_ex, "test_events": f_ev, "wallets": fresh["wallet"].n_unique(),
                 "note": f"fresh: {f_n} trades / {f_ev} events; comparison = wallets older than 90 days: {o_n} trades"})
    rows.append({"hypothesis": "H6 Win rate (last 30 days) predicts later edge", "spearman": rho_wr, "wallets": wr.height})
    hyp = pl.DataFrame(rows, infer_schema_length=None)
    hyp.write_parquet(d / "kb_hypotheses.parquet")
    summ = {"cutoff": cutoff.isoformat(), "specialists": spec.height, "generalists": gen.height,
            "specialties": dict(spec.group_by("spec_cat").len().iter_rows()), "h6_spearman_winrate": rho_wr,
            "h5_fresh": [f_ex, f_n, f_ev], "h5_old": [o_ex, o_n, o_ev]}
    (d / "kb_summary.json").write_text(json.dumps(summ, indent=1, default=str))
    pl.Config.set_tbl_width_chars(220)
    print(hyp.select([c for c in ["hypothesis", "wallets", "test_events", "spearman", "p_value", "top_q_edge", "top_q_lo",
                                  "top_q_hi", "bottom_q_edge", "copy_cost", "top_q_net"] if c in hyp.columns]))
    print(json.dumps(summ, indent=1, default=str))


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 21)
