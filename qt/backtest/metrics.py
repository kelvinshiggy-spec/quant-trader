"""Performance metrics and statistical tests (no metric is used alone to promote)."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
from scipy import stats


def trade_metrics(t: pd.DataFrame) -> dict:
    if t is None or t.empty:
        return {"trades": 0}
    pnl = t["pnl"].to_numpy(float)
    r = t["r"].to_numpy(float)
    wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
    gp, gl = wins.sum(), -losses.sum()
    streak = mx = 0
    for x in pnl:
        streak = streak + 1 if x <= 0 else 0
        mx = max(mx, streak)
    top = np.sort(pnl)[::-1]
    k = max(1, int(math.ceil(len(pnl) * 0.05)))
    gp_ex = gp - top[:k][top[:k] > 0].sum()
    sessions = t["session"].nunique() if "session" in t else None
    return {
        "trades": int(len(t)),
        "net_pnl": float(pnl.sum()),
        "expectancy": float(pnl.mean()),
        "expectancy_r": float(r.mean()),
        "median_r": float(np.median(r)),
        "win_rate": float((pnl > 0).mean()),
        "avg_win": float(wins.mean()) if len(wins) else 0.0,
        "avg_loss": float(losses.mean()) if len(losses) else 0.0,
        "profit_factor": float(gp / gl) if gl > 0 else float("inf"),
        "pf_ex_top5pct": float(gp_ex / gl) if gl > 0 else float("inf"),
        "max_consec_losses": int(mx),
        "fees": float(t["fees"].sum()),
        "avg_bars_held": float(t["bars_held"].mean()),
        "trades_per_session": float(len(t) / sessions) if sessions else None,
        "exit_reasons": t["exit_reason"].value_counts().to_dict(),
    }


def equity_metrics(eq: pd.Series, start_cash: float) -> dict:
    eq = eq.dropna()
    if eq.empty:
        return {}
    ret = eq.pct_change().dropna()
    peak = eq.cummax()
    dd = (eq - peak) / peak
    # recovery: longest stretch below a previous peak (in sessions)
    under = (eq < peak).astype(int).to_numpy()
    longest = cur = 0
    for u in under:
        cur = cur + 1 if u else 0
        longest = max(longest, cur)
    sd = ret.std(ddof=1)
    downside = ret[ret < 0].std(ddof=1)
    return {
        "final_equity": float(eq.iloc[-1]),
        "total_return_pct": float((eq.iloc[-1] / start_cash - 1) * 100),
        "max_drawdown_pct": float(-dd.min() * 100),
        "sharpe": float(ret.mean() / sd * np.sqrt(252)) if sd and sd > 0 else 0.0,
        "sortino": float(ret.mean() / downside * np.sqrt(252)) if downside and downside > 0 else 0.0,
        "longest_underwater_sessions": int(longest),
        "sessions": int(len(eq)),
    }


def bootstrap_mean(x: np.ndarray, n: int = 2000, seed: int = 0) -> dict:
    """Bootstrap CI and one-sided p-value for mean > 0."""
    x = np.asarray(x, float)
    if len(x) < 5:
        return {"mean": float(x.mean()) if len(x) else 0.0, "lo": np.nan, "hi": np.nan, "p_gt0": 1.0}
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(x), size=(n, len(x)))
    means = x[idx].mean(axis=1)
    centered = means - x.mean()
    p = float((centered >= x.mean()).mean())  # H0: mean <= 0
    return {"mean": float(x.mean()), "lo": float(np.percentile(means, 2.5)),
            "hi": float(np.percentile(means, 97.5)), "p_gt0": max(p, 1.0 / n)}


def ttest_gt0(x) -> float:
    x = np.asarray(x, float)
    if len(x) < 5 or np.std(x) == 0:
        return 1.0
    t, p = stats.ttest_1samp(x, 0.0)
    return float(p / 2 if t > 0 else 1 - p / 2)


def holm(pvals: dict[str, float]) -> dict[str, float]:
    """Holm-Bonferroni adjusted p-values (controls family-wise error)."""
    items = sorted(pvals.items(), key=lambda kv: kv[1])
    m = len(items)
    adj, running = {}, 0.0
    for i, (k, p) in enumerate(items):
        v = min(1.0, (m - i) * p)
        running = max(running, v)
        adj[k] = running
    return adj


def monte_carlo_drawdown(r_trades: np.ndarray, risk_per_trade: float = 0.005, n_trades: int = 250,
                         sims: int = 3000, seed: int = 1, start: float = 100.0,
                         ruin_dd: float = 0.10) -> dict:
    """Resample trade R-multiples to estimate drawdown/loss distributions at the live sizing."""
    r = np.asarray(r_trades, float)
    if len(r) < 20:
        return {}
    rng = np.random.default_rng(seed)
    samp = r[rng.integers(0, len(r), size=(sims, n_trades))]
    growth = 1 + samp * risk_per_trade
    eq = start * np.cumprod(growth, axis=1)
    peak = np.maximum.accumulate(np.concatenate([np.full((sims, 1), start), eq], axis=1), axis=1)[:, 1:]
    dd = ((peak - eq) / peak).max(axis=1)
    final = eq[:, -1]
    return {
        "n_trades": n_trades,
        "median_final": float(np.median(final)),
        "p5_final": float(np.percentile(final, 5)),
        "p95_final": float(np.percentile(final, 95)),
        "median_max_dd_pct": float(np.median(dd) * 100),
        "p95_max_dd_pct": float(np.percentile(dd, 95) * 100),
        "prob_dd_over_limit": float((dd >= ruin_dd).mean()),
        "prob_loss": float((final < start).mean()),
    }
