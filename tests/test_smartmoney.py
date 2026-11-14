"""Scoring sanity: planted skill is found, noise is not, and scores never see the future."""
from datetime import datetime, timedelta, timezone

import numpy as np
import polars as pl

from pmkt import smartmoney as sm

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)


def synth(n_wallets=200, n_events=60, skilled=20, edge=0.12, seed=1):
    rng = np.random.default_rng(seed)
    rows = []
    for w in range(n_wallets):
        sk = w < skilled
        for k in range(n_events):
            p = rng.uniform(0.1, 0.9)
            q = min(0.99, p + edge) if sk else p          # true win probability
            x = float(rng.random() < q)
            t_in = T0 + timedelta(days=float(rng.uniform(0, 80)))
            n = 100.0
            rows.append({"wallet": f"w{w}", "token_id": f"t{w}_{k}", "condition_id": f"c{k}", "event_id": f"e{w}_{k}",
                         "title": "", "p": p, "n": n, "cap": n * p, "pnl": n * (x - p), "e": x - p, "x": x,
                         "binary": True, "t_entry": t_in, "t_out": t_in + timedelta(days=1), "status": "CLOSED"})
    return pl.DataFrame(rows)


def test_point_in_time_excludes_future():
    pos = synth(n_wallets=5, n_events=40)
    cut = T0 + timedelta(days=40)
    s = sm.score(pos, cut, min_events=1)
    used = pos.filter((pl.col("t_out") < cut) & (pl.col("t_entry") < cut)).group_by("wallet").agg(pl.col("event_id").n_unique())
    assert dict(s.select("wallet", "events").iter_rows()) == dict(used.iter_rows())


def test_walk_forward_finds_planted_skill():
    res, _ = sm.walk_forward(synth(), T0 + timedelta(days=40), n_perm=300)
    r = {x["definition"]: x for x in res.to_dicts()}
    assert r["calibration"]["spearman"] > 0.2 and r["calibration"]["p_value"] < 0.01
    assert r["calibration"]["top_q_edge"] > r["calibration"]["bottom_q_edge"]


def test_walk_forward_no_skill_no_signal():
    res, _ = sm.walk_forward(synth(skilled=0, seed=3), T0 + timedelta(days=40), n_perm=300)
    r = {x["definition"]: x for x in res.to_dicts()}
    assert r["calibration"]["p_value"] > 0.01 and abs(r["calibration"]["top_q_edge"]) < 0.05
