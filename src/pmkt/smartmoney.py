"""Smart-money / insider scoring with a walk-forward test of every definition.

Input: per-wallet resolved positions (Data API v2 CLOSED + resolved REDEEMABLE; see scripts/fetch_smartmoney.py).
A position's economics:  p = avg entry price, n = shares bought, edge per share e = total_pnl / n  (= x - p,
where x is the realised exit value per share: 1 / 0 at resolution, or the sale price). capital = n * p.

Point-in-time rule: a score as of T uses only positions whose outcome was known before T (t_out < T) and that
were entered before T. The walk-forward test scores at T and measures performance on positions ENTERED after T.
Dependence: markets in one event are correlated, so per-wallet statistics are clustered by event.

Definitions (each is a hypothesis, tested head to head):
  pnl_skill     ROI t-stat across events: mean(event pnl / event capital) / se                (realised-PnL skill)
  calibration   sum(x - p) / sqrt(sum p(1-p)) over positions resolved at 0/1               (beats implied prob.)
  longshot      calibration restricted to entries at p < 0.25                             (early / informed buying)
  conviction    capital-weighted edge per share, shrunk toward 0 by k/(k + 20) events     (size where it matters)
Labels (not scores): market maker (from our fills' maker share), insider signature (fresh wallet, concentrated, large).
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

import numpy as np
import polars as pl

MIN_EVENTS = 10
DEFINITIONS = ["pnl_skill", "calibration", "longshot", "conviction"]


def positions_table(hist: pl.DataFrame) -> pl.DataFrame:
    """Normalise raw API rows to one row per (wallet, token) with entry/exit economics and timestamps."""
    h = hist.unique(subset=["wallet", "token_id"], keep="first")
    end = pl.col("end_date").str.to_datetime(strict=False, time_zone="UTC")
    h = h.with_columns(
        p=pl.col("avg_price").cast(pl.Float64), n=pl.col("total_size").cast(pl.Float64),
        pnl=pl.col("total_pnl").cast(pl.Float64),
        t_entry=pl.from_epoch(pl.col("first_entry_at").cast(pl.Int64), time_unit="s").dt.replace_time_zone("UTC"),
        t_last=pl.from_epoch(pl.col("last_event_at").cast(pl.Int64), time_unit="s").dt.replace_time_zone("UTC"),
        t_end=end,
        cp=pl.col("current_price").cast(pl.Float64),
    )
    resolved = (pl.col("status") == "CLOSED") | ((pl.col("status") == "REDEEMABLE") &
                                                 ((pl.col("cp") <= 0.02) | (pl.col("cp") >= 0.98)))
    h = h.filter(resolved & (pl.col("n") > 0) & pl.col("p").is_between(0.01, 0.99, closed="none")
                 & pl.col("t_entry").is_not_null())
    h = h.with_columns(
        e=(pl.col("pnl") / pl.col("n")).clip(-1.0, 1.0),
        cap=pl.col("n") * pl.col("p"),
        # outcome known: last event for exits, market end date for held-to-resolution
        t_out=pl.when(pl.col("status") == "CLOSED").then(pl.col("t_last")).otherwise(pl.coalesce("t_end", "t_last")),
    ).with_columns(x=(pl.col("p") + pl.col("e")).clip(0.0, 1.0))
    h = h.with_columns(binary=(pl.col("x") <= 0.02) | (pl.col("x") >= 0.98))
    return h.filter(pl.col("cap") >= 5.0).select(
        "wallet", "token_id", "condition_id", "event_id", "title", "p", "n", "cap", "pnl", "e", "x", "binary",
        "t_entry", "t_out", "status")


def score(pos: pl.DataFrame, as_of: datetime | None = None, min_events: int = MIN_EVENTS) -> pl.DataFrame:
    """Score every wallet with every definition using positions resolved and entered before `as_of`."""
    if as_of is not None:
        pos = pos.filter((pl.col("t_out") < as_of) & (pl.col("t_entry") < as_of))
    ev = (pos.group_by("wallet", "event_id")
          .agg(pnl=pl.col("pnl").sum(), cap=pl.col("cap").sum(),
               cal_num=pl.when(pl.col("binary")).then(pl.col("x") - pl.col("p")).otherwise(0.0).sum(),
               cal_var=pl.when(pl.col("binary")).then(pl.col("p") * (1 - pl.col("p"))).otherwise(0.0).sum(),
               ls_num=pl.when(pl.col("binary") & (pl.col("p") < 0.25)).then(pl.col("x") - pl.col("p")).otherwise(0.0).sum(),
               ls_var=pl.when(pl.col("binary") & (pl.col("p") < 0.25)).then(pl.col("p") * (1 - pl.col("p"))).otherwise(0.0).sum(),
               ecap=(pl.col("e") * pl.col("cap")).sum())
          .with_columns(roi=pl.col("pnl") / pl.col("cap")))
    w = ev.group_by("wallet").agg(
        events=pl.len(), capital=pl.col("cap").sum(), pnl=pl.col("pnl").sum(),
        roi_mean=pl.col("roi").mean(), roi_sd=pl.col("roi").std(),
        cal_num=pl.col("cal_num").sum(), cal_var=pl.col("cal_var").sum(),
        ls_num=pl.col("ls_num").sum(), ls_var=pl.col("ls_var").sum(), ecap=pl.col("ecap").sum(),
        best_event_pnl=pl.col("pnl").max())
    k = pl.col("events")
    w = w.with_columns(
        pnl_skill=pl.col("roi_mean") / (pl.col("roi_sd") / k.sqrt()).clip(lower_bound=1e-6),
        calibration=pl.when(pl.col("cal_var") > 0).then(pl.col("cal_num") / pl.col("cal_var").sqrt()),
        longshot=pl.when(pl.col("ls_var") > 0.5).then(pl.col("ls_num") / pl.col("ls_var").sqrt()),
        conviction=(pl.col("ecap") / pl.col("capital")) * k / (k + 20),
        roi=pl.col("pnl") / pl.col("capital"),
        roi_ex_best=(pl.col("pnl") - pl.col("best_event_pnl")) / pl.col("capital"),
    )
    return w.filter(k >= min_events)


def _spearman(a: np.ndarray, b: np.ndarray) -> float:
    ra, rb = a.argsort().argsort(), b.argsort().argsort()
    return float(np.corrcoef(ra, rb)[0, 1])


def walk_forward(pos: pl.DataFrame, cutoff: datetime, min_test_events: int = 5, n_perm: int = 2000,
                 seed: int = 7) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Score at `cutoff`; outcome = each wallet's edge on positions ENTERED after the cutoff (and resolved).
    Returns (per-definition results, per-wallet panel)."""
    train = score(pos, cutoff)
    test = pos.filter(pl.col("t_entry") >= cutoff)
    tw = (test.group_by("wallet", "event_id").agg(e=(pl.col("e") * pl.col("cap")).sum() / pl.col("cap").sum(),
                                                  cap=pl.col("cap").sum(),
                                                  fee_ps=(0.05 * (1 - pl.col("p")) * pl.col("cap")).sum() / pl.col("cap").sum())
          .group_by("wallet").agg(test_events=pl.len(), test_edge=pl.col("e").mean(),
                                  test_capital=pl.col("cap").sum(), test_fee=pl.col("fee_ps").mean()))
    panel = train.join(tw, on="wallet", how="inner").filter(pl.col("test_events") >= min_test_events)
    rng = np.random.default_rng(seed)
    res = []
    y = panel["test_edge"].to_numpy()
    for d in DEFINITIONS:
        sub = panel.filter(pl.col(d).is_not_null() & pl.col(d).is_finite())
        if sub.height < 30:
            res.append({"definition": d, "wallets": sub.height, "note": "too few wallets"})
            continue
        s, yy = sub[d].to_numpy(), sub["test_edge"].to_numpy()
        rho = _spearman(s, yy)
        perm = np.array([_spearman(s, rng.permutation(yy)) for _ in range(n_perm)])
        q = np.quantile(s, [0.2, 0.8])
        top, bot = yy[s >= q[1]], yy[s <= q[0]]
        fee_top = sub.filter(pl.col(d) >= q[1])["test_fee"].mean()
        # wallet-level bootstrap CI of the top-quintile mean test edge
        boots = [rng.choice(top, len(top)).mean() for _ in range(n_perm)]
        res.append({
            "definition": d, "wallets": sub.height, "test_events": int(sub["test_events"].sum()),
            "spearman": rho, "p_value": float((np.abs(perm) >= abs(rho)).mean()),
            "top_q_edge": float(top.mean()), "top_q_lo": float(np.quantile(boots, 0.025)),
            "top_q_hi": float(np.quantile(boots, 0.975)), "bottom_q_edge": float(bot.mean()),
            "spread": float(top.mean() - bot.mean()), "all_edge": float(y.mean()),
            "copy_cost": float(fee_top + 0.02),  # taker fee ~0.05*(1-p) + ~1c spread + ~1c latency slippage
            "top_q_net": float(top.mean() - fee_top - 0.02),
        })
    out = pl.DataFrame(res, infer_schema_length=None)
    # Benjamini-Hochberg across definitions
    if "p_value" in out.columns:
        pv = out["p_value"].fill_null(1.0).to_numpy()
        order = pv.argsort()
        m = len(pv)
        adj = np.empty(m)
        prev = 1.0
        for rank, i in reversed(list(enumerate(order, 1))):
            prev = min(prev, pv[i] * m / rank)
            adj[i] = prev
        out = out.with_columns(p_bh=pl.Series(adj))
    return out, panel


def insider_flags(holders: pl.DataFrame, stats: pl.DataFrame, now: datetime | None = None,
                  max_age_days: float = 30, min_value: float = 5000, max_trades: int = 50) -> pl.DataFrame:
    """Insider *signature* (not proof): young account, few trades, large position in this market.
    Monitoring only: legal/ToS risk applies to acting on suspected inside information."""
    now = now or datetime.now(timezone.utc)
    st = stats.select("wallet", "join_date", "trades").with_columns(
        age_days=(pl.lit(now.timestamp()) - pl.col("join_date").cast(pl.Float64)) / 86400)
    h = holders.join(st, left_on="proxy_wallet", right_on="wallet", how="left")
    return h.with_columns(insider_sig=(pl.col("age_days") <= max_age_days) & (pl.col("current_value") >= min_value)
                          & (pl.col("trades").fill_null(0) <= max_trades))


def maker_share(fills: pl.DataFrame) -> pl.DataFrame:
    return fills.group_by("wallet").agg(fills=pl.len(), maker_share=(pl.col("role") == "maker").mean())


def safe(x):
    return None if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))) else x
