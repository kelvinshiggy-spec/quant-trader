"""Autonomous research run: every strategy, walk-forward, holdout, stress tests,
multiple-testing correction, Monte Carlo, $100 account simulation, registry.

Run: python -m qt.research.runner [--workers 4] [--symbols SPY,QQQ]
Resource use is isolated from the live engine (separate process / container).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

from ..backtest.engine import BTConfig, run_backtest
from ..backtest.metrics import equity_metrics, holm, monte_carlo_drawdown, trade_metrics
from ..config import RiskLimits, load_settings
from ..costs import CostModel
from ..data.historical import HistoricalData
from ..db import DB
from ..markettime import now_utc
from ..regime import coarse, daily_regimes
from ..strategies.library import ALL_STRATEGIES, BY_NAME
from ..universe import CRYPTO, INTRADAY_UNIVERSE, SECTOR_ETFS
from .datasets import Datasets
from .walkforward import (add_regime, allowed_regimes, choose_on_dev, make_windows, slice_trades, summarize,
                          walk_forward, _clean)

log = logging.getLogger("research")

DEV_START, DEV_END = dt.date(2021, 1, 1), dt.date(2024, 12, 31)
# Round-2 daily strategies (O-S) use the longer daily history (pre-registered before their first run).
DEV2_START = dt.date(2017, 1, 1)


def dev_start_for(inst) -> dt.date:
    return DEV2_START if getattr(inst, "research_round", 1) >= 2 else DEV_START


def in_universe(inst, sym: str) -> bool:
    return not inst.universe or sym in inst.universe
HOLDOUT_START = dt.date(2025, 1, 1)
META_FEATURES = ["relvol", "adx", "rsi14", "atr_pct", "minutes", "gap", "bbw_rank", "rs6"]

CRITERIA = {
    "min_oos_trades": 60, "max_holm_p": 0.05, "min_oos_pf": 1.3, "min_pf_ex_top": 1.0,
    "min_holdout_trades": 20, "min_holdout_pf": 1.1, "shadow_max_raw_p": 0.10, "shadow_min_pf": 1.1,
}


def _pkey(params: dict) -> str:
    return json.dumps(params, sort_keys=True)


def _run_param_set(strategy_cls, params, frames, stress=(1.0, 2.0)):
    out = {}
    st = strategy_cls(params=params)
    sigs = {s: st.signals(f) for s, f in frames.items() if not f.empty}
    for sc in stress:
        cfg = BTConfig(capital_constrained=False, costs=CostModel(stress=sc))
        r = run_backtest(st, frames, cfg, signals=sigs)
        out[sc] = r.trades_df()
    return out, sigs


def _attach_meta(trades: pd.DataFrame, frames: dict) -> pd.DataFrame:
    if trades.empty:
        return trades
    rows = []
    for _, t in trades.iterrows():
        f = frames[t["symbol"]]
        ts = pd.Timestamp(t["signal_time"])
        ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts
        try:
            r = f.loc[ts]
            rows.append({k: float(r[k]) if k in r and pd.notna(r[k]) else np.nan for k in META_FEATURES})
        except KeyError:
            rows.append({k: np.nan for k in META_FEATURES})
    return pd.concat([trades.reset_index(drop=True), pd.DataFrame(rows)], axis=1)


def _intraday_worker(args):
    sym, data_dir, start, end, daily_start = args
    s = load_settings()
    hd = HistoricalData(s.api_key, s.api_secret, Path(data_dir) / "cache", feed=s.research_feed)
    ds = Datasets(hd, start, end, daily_start, Path(data_dir) / "features")
    f = ds.intraday_feats([sym])[sym]
    frames = {sym: f}
    res = {}
    for cls in ALL_STRATEGIES:
        inst = cls()
        if inst.timeframe != "5Min":
            continue
        psets = cls.param_sets()
        if "relvol" in inst.defaults:  # ablation: remove the relative-volume confirmation
            ab = dict(inst.defaults)
            ab["relvol"] = 0.0
            psets = psets + [ab]
        for p in psets:
            tr, _ = _run_param_set(cls, p, frames)
            res[(inst.name, _pkey(p))] = {k: _attach_meta(v, frames) if k == 1.0 else v for k, v in tr.items()}
    return sym, res


def _merge(results: list[tuple[str, dict]]) -> dict:
    merged: dict = {}
    for _, res in results:
        for key, by_stress in res.items():
            for sc, df in by_stress.items():
                merged.setdefault(key, {}).setdefault(sc, []).append(df)
    return {k: {sc: pd.concat(v, ignore_index=True) if v else pd.DataFrame() for sc, v in d.items()}
            for k, d in merged.items()}


COMPACT = ["open", "high", "low", "close", "atr", "adv", "bars_left", "session"]


def compact_inputs(strategy_name: str, params: dict, frames_iter, start, end):
    """Compute signals on full history (warm-up), keep only the period's rows and the
    columns the simulator needs. Keeps memory small enough for a 1 GB server."""
    st = BY_NAME[strategy_name](params=params)
    fr, sg = {}, {}
    for sym, f in frames_iter:
        if f is None or f.empty:
            continue
        sig = st.signals(f)
        d = pd.to_datetime(f["session"]).dt.date
        m = ((d >= start) & (d <= end)).to_numpy()
        if not m.any():
            continue
        cols = [c for c in COMPACT if c in f.columns]
        fr[sym] = f.loc[m, cols].copy()
        sg[sym] = sig.loc[m].copy()
    return st, fr, sg


def constrained_sim(strategy_name: str, params: dict, frames_iter, start, end, allowed=None,
                    regime_by_session=None, account_type="cash") -> tuple[dict, object]:
    st, fr, sg = compact_inputs(strategy_name, params, frames_iter, start, end)
    cfg = BTConfig(capital_constrained=True, account_type=account_type, start=start, end=end,
                   allowed_regimes=allowed, regime_by_session=regime_by_session)
    r = run_backtest(st, fr, cfg, signals=sg)
    t = r.trades_df()
    out = {"equity": equity_metrics(r.equity_daily, cfg.starting_cash), "trades": trade_metrics(t),
           "guardian_rejections_top": dict(sorted(r.guardian_rejections.items(), key=lambda kv: -kv[1])[:5]),
           "drawdown_halt_on": r.halted_on}
    return _clean(out), r


def run(workers: int = 4, symbols: list[str] | None = None, end: dt.date | None = None,
        include: set | None = None) -> dict:
    s = load_settings()
    t0 = time.time()
    run_id = now_utc().strftime("%Y%m%dT%H%M%SZ")
    end = end or (dt.date.today() - dt.timedelta(days=2))
    data_dir = s.data_dir
    hd = HistoricalData(s.api_key, s.api_secret, data_dir / "cache", feed=s.research_feed)
    ds = Datasets(hd, DEV_START, end, dt.date(2016, 1, 1), data_dir / "features")
    db = DB(s.db_path)
    daily = ds.daily_raw()
    regimes = daily_regimes(daily["SPY"], {k: daily[k] for k in SECTOR_ETFS if k in daily})
    reg_map = regimes["label"].to_dict()
    symbols = symbols or INTRADAY_UNIVERSE
    windows = make_windows(DEV_START, DEV_END)
    log.info("research %s: %d symbols, %d WF windows, holdout %s..%s", run_id, len(symbols), len(windows),
             HOLDOUT_START, end)

    # ---- 1) signal-level trades ----
    jobs = [(sym, str(data_dir), DEV_START, end, dt.date(2016, 1, 1)) for sym in symbols]
    if "SPY" not in symbols:
        ds.intraday_feats(["SPY"])
    else:
        ds.intraday_feats(["SPY"])  # build benchmark features first (workers read the cache)
    with ProcessPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(_intraday_worker, jobs))
    all_trades = _merge(results)
    log.info("intraday signal backtests done in %.0fs", time.time() - t0)

    dfe = ds.daily_feats()
    cfe = ds.crypto_feats()
    other_frames = {"1Day": dfe, "1Hour": cfe}
    for cls in ALL_STRATEGIES:
        inst = cls()
        if inst.timeframe == "5Min":
            continue
        frames = {k: v for k, v in other_frames[inst.timeframe].items() if in_universe(inst, k)}
        for p in cls.param_sets():
            tr, _ = _run_param_set(cls, p, frames)
            all_trades[(inst.name, _pkey(p))] = tr

    # ---- 2) per-strategy walk-forward / holdout ----
    reports, pvals = {}, {}
    for cls in ALL_STRATEGIES:
        inst = cls()
        if include and inst.name not in include:
            continue
        keys = [_pkey(p) for p in cls.param_sets()]
        tbp = {k: add_regime(all_trades.get((inst.name, k), {}).get(1.0, pd.DataFrame()), reg_map) for k in keys}
        tbp_s2 = {k: all_trades.get((inst.name, k), {}).get(2.0, pd.DataFrame()) for k in keys}
        default = _pkey(inst.defaults) if _pkey(inst.defaults) in tbp else keys[0]
        dev_a = dev_start_for(inst)
        swin = windows if dev_a == DEV_START else make_windows(dev_a, DEV_END)
        for gated in (False, True):
            variant = f"{inst.name}{'+regime' if gated else ''}"
            oos, wlog = walk_forward(tbp, swin, default, gated=gated)
            chosen = choose_on_dev(tbp, dev_a, DEV_END, default)
            hold = slice_trades(tbp[chosen], HOLDOUT_START, end)
            allowed = None
            if gated:
                allowed = allowed_regimes(slice_trades(tbp[chosen], dev_a, DEV_END))
                hold = hold[hold["regime_coarse"].isin(allowed)] if hold is not None and not hold.empty else hold
            # stress 2x on the same OOS selections
            s2 = []
            for w in wlog:
                a, b = w["test"].split("..")
                x = slice_trades(tbp_s2[w["chosen"]], dt.date.fromisoformat(a), dt.date.fromisoformat(b))
                if x is not None and not x.empty:
                    if gated and w["allowed_regimes"] is not None:
                        x = add_regime(x, reg_map)
                        x = x[x["regime_coarse"].isin(set(w["allowed_regimes"]))]
                    s2.append(x)
            s2df = pd.concat(s2) if s2 else pd.DataFrame()
            oos_sum = summarize(oos)
            rep = {
                "strategy": inst.name, "variant": variant, "letter": inst.letter, "version": inst.version,
                "timeframe": inst.timeframe, "overnight": inst.overnight, "asset_class": inst.asset_class,
                "description": inst.description, "exit_plan": inst.exit_plan().describe() if True else "",
                "oos": oos_sum, "oos_stress2x": summarize(s2df), "holdout": summarize(hold),
                "chosen_params": json.loads(chosen), "allowed_regimes": sorted(allowed) if allowed is not None else None,
                "windows": wlog,
                "monte_carlo": _clean(monte_carlo_drawdown(oos["r"].to_numpy())) if not oos.empty else {},
                "dev_full": summarize(slice_trades(tbp[chosen], dev_a, DEV_END)),
                "dev_period": f"{dev_a}..{DEV_END}", "research_round": getattr(inst, "research_round", 1),
            }
            # filter ablation (relative-volume confirmation removed)
            if "relvol" in inst.defaults:
                ab = dict(inst.defaults)
                ab["relvol"] = 0.0
                abt = add_regime(all_trades.get((inst.name, _pkey(ab)), {}).get(1.0, pd.DataFrame()), reg_map)
                base_t = tbp.get(default)
                rep["ablation_relvol"] = {
                    "with_filter_dev": summarize(slice_trades(base_t, DEV_START, DEV_END)),
                    "without_filter_dev": summarize(slice_trades(abt, DEV_START, DEV_END)),
                }
            reports[variant] = rep
            pvals[variant] = oos_sum.get("p_value", 1.0) if oos_sum.get("trades", 0) else 1.0

    adj = holm(pvals)
    # ---- 3) decisions ----
    C = CRITERIA
    for v, rep in reports.items():
        o, h, s2 = rep["oos"], rep["holdout"], rep["oos_stress2x"]
        rep["holm_p"] = adj.get(v, 1.0)
        fails = []
        if o.get("trades", 0) < C["min_oos_trades"]:
            fails.append(f"only {o.get('trades', 0)} OOS trades (< {C['min_oos_trades']})")
        if rep["holm_p"] > C["max_holm_p"]:
            fails.append(f"not significant after multiple-testing correction (Holm p={rep['holm_p']:.3f})")
        if (o.get("profit_factor") or 0) < C["min_oos_pf"]:
            fails.append(f"OOS profit factor {o.get('profit_factor', 0) or 0:.2f} < {C['min_oos_pf']}")
        if (o.get("pf_ex_top5pct") or 0) < C["min_pf_ex_top"]:
            fails.append("edge depends on a few outsized winners")
        if (s2.get("expectancy_r") or -1) <= 0:
            fails.append("not profitable at 2x cost stress")
        if h.get("trades", 0) < C["min_holdout_trades"] or (h.get("profit_factor") or 0) < C["min_holdout_pf"] \
                or (h.get("expectancy_r") or -1) <= 0:
            fails.append(f"holdout failed (trades={h.get('trades', 0)}, PF={h.get('profit_factor') or 0:.2f}, "
                         f"expR={h.get('expectancy_r') or 0:.3f})")
        if not fails:
            status = "validated"
        elif (o.get("p_value") or 1) < C["shadow_max_raw_p"] and (o.get("profit_factor") or 0) >= C["shadow_min_pf"] \
                and o.get("trades", 0) >= 30:
            status = "shadow"
        else:
            status = "rejected"
        rep["status"], rep["fail_reasons"] = status, fails

    # ---- 4) realistic $100 simulations (holdout) for every strategy's best variant ----
    best_variant = {}
    for v, rep in reports.items():
        n = rep["strategy"]
        if n not in best_variant or (rep["oos"].get("expectancy_r") or -9) > (reports[best_variant[n]]["oos"].get("expectancy_r") or -9):
            best_variant[n] = v
    regmap_coarse = {k: coarse(x) for k, x in reg_map.items()}
    for n, v in best_variant.items():
        rep = reports[v]
        inst = BY_NAME[n]()
        if inst.timeframe == "5Min":
            frames_iter = ((sym, ds.intraday_feats([sym])[sym]) for sym in symbols)
        else:
            fr = {k: f for k, f in other_frames[inst.timeframe].items() if in_universe(inst, k)}
            frames_iter = iter(fr.items())
        allowed = set(rep["allowed_regimes"]) if rep["allowed_regimes"] is not None else None
        sim, _ = constrained_sim(n, rep["chosen_params"], frames_iter, HOLDOUT_START, end, allowed, regmap_coarse,
                                 s.account_type)
        rep["account_sim_holdout"] = sim

    # ---- 5) meta-label ML experiment (research only) ----
    meta = meta_model_experiment(all_trades, windows)

    summary = {"run_id": run_id, "created_at": now_utc().isoformat(), "dev": f"{DEV_START}..{DEV_END}",
               "holdout": f"{HOLDOUT_START}..{end}", "symbols": symbols, "criteria": CRITERIA,
               "tests_corrected": len(pvals), "duration_s": round(time.time() - t0),
               "meta_model": meta, "reports": reports,
               "spy_buy_hold_holdout_pct": _bh(daily["SPY"], HOLDOUT_START, end)}
    persist(db, summary, data_dir, auto_approve=s.auto_approve_validated)
    return summary


def _bh(d: pd.DataFrame, a, b) -> float:
    x = d[(d.index.date >= a) & (d.index.date <= b)]["close"]
    return float((x.iloc[-1] / x.iloc[0] - 1) * 100) if len(x) > 1 else 0.0


def meta_model_experiment(all_trades: dict, windows) -> dict:
    """Does a logistic-regression meta-label filter improve OOS expectancy over 'take every signal'?"""
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    frames = []
    for (name, pk), d in all_trades.items():
        t = d.get(1.0)
        if t is not None and not t.empty and all(c in t for c in META_FEATURES):
            frames.append(t.assign(strat=name))
    if not frames:
        return {"status": "no data"}
    t = pd.concat(frames, ignore_index=True)
    t["sess"] = pd.to_datetime(t["session"]).dt.date
    X_cols = META_FEATURES
    res = []
    for w in windows:
        tr = t[(t["sess"] >= w.train_start) & (t["sess"] <= w.train_end)].dropna(subset=X_cols)
        te = t[(t["sess"] >= w.test_start) & (t["sess"] <= w.test_end)].dropna(subset=X_cols)
        if len(tr) < 500 or len(te) < 50:
            continue
        m = make_pipeline(StandardScaler(), LogisticRegression(C=0.1, max_iter=500))
        m.fit(tr[X_cols], (tr["r"] > 0).astype(int))
        p = m.predict_proba(te[X_cols])[:, 1]
        brier = float(np.mean((p - (te["r"] > 0)) ** 2))
        base_rate = float((tr["r"] > 0).mean())
        sel = te[p > base_rate + 0.05]
        res.append({"window": str(w.test_start), "all_exp_r": float(te["r"].mean()), "n_all": len(te),
                    "filtered_exp_r": float(sel["r"].mean()) if len(sel) else None, "n_filtered": len(sel),
                    "brier": brier, "brier_baseline": float(np.mean((base_rate - (te["r"] > 0)) ** 2))})
    if not res:
        return {"status": "insufficient data"}
    df = pd.DataFrame(res)
    improved = df.dropna()
    return _clean({"windows": res,
                   "mean_all_exp_r": float(df["all_exp_r"].mean()),
                   "mean_filtered_exp_r": float(improved["filtered_exp_r"].mean()) if len(improved) else None,
                   "brier_better_than_baseline_share": float((df["brier"] < df["brier_baseline"]).mean()),
                   "decision": "keep researching; NOT used in production unless it beats the simple baseline OOS"})


def persist(db: DB, summary: dict, data_dir: Path, auto_approve: bool = False) -> None:
    run_id = summary["run_id"]
    for v, rep in summary["reports"].items():
        db.insert("experiments", {
            "created_at": summary["created_at"], "run_id": run_id,
            "hypothesis": rep["description"], "strategy": v, "version": rep["version"],
            "params": rep["chosen_params"], "dataset": f"Alpaca SIP adj. 5Min/1Day + crypto 1Hour; {len(summary['symbols'])} symbols",
            "train_period": f"rolling 12m within {summary['dev']}", "validation_period": "rolling 3m walk-forward",
            "test_period": summary["holdout"], "results": rep,
            "limitations": "survivorship-biased fixed universe; estimated spreads; bar-level stop fills; IEX live vs SIP research",
            "decision": rep["status"] + ("" if not rep["fail_reasons"] else ": " + "; ".join(rep["fail_reasons"])),
            "status": rep["status"]})
        prev = db.one("SELECT * FROM strategy_status WHERE strategy=?", (v,))
        approved = int(prev["approved"]) if prev else 0
        approved_at = prev["approved_at"] if prev else None
        status = rep["status"]
        if prev and prev["approved"] and status != "validated":
            status = "suspended"
        if auto_approve and status == "validated" and not approved:
            approved, approved_at = 1, summary["created_at"]
            db.event("important", "research", f"{v} passed validation and was auto-approved for paper trading",
                     {"run_id": run_id, "oos": rep["oos"], "holdout": rep["holdout"]})
        hist = json.loads(prev["history"]) if prev and prev["history"] else []
        hist.append({"run_id": run_id, "status": rep["status"], "params": rep["chosen_params"]})
        db.upsert("strategy_status", {
            "strategy": v, "version": rep["version"], "params": json.dumps(rep["chosen_params"]), "status": status,
            "approved": approved, "approved_at": approved_at, "champion": 0,
            "oos_metrics": {**rep["oos"], "holm_p": rep["holm_p"]}, "holdout_metrics": rep["holdout"],
            "forward_metrics": prev["forward_metrics"] if prev else None,
            "allowed_regimes": rep["allowed_regimes"], "reason": "; ".join(rep["fail_reasons"]) or "passed all criteria",
            "updated_at": summary["created_at"], "history": hist[-30:]}, "strategy")
    # champion = best validated variant by OOS expectancy with acceptable drawdown
    val = [(v, r) for v, r in summary["reports"].items() if r["status"] == "validated"]
    db.execute("UPDATE strategy_status SET champion=0")
    if val:
        champ = max(val, key=lambda x: x[1]["oos"].get("expectancy_r") or -9)[0]
        db.execute("UPDATE strategy_status SET champion=1 WHERE strategy=?", (champ,))
        summary["champion"] = champ
    else:
        summary["champion"] = None
    out = Path(data_dir) / "reports"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"research_{run_id}.json").write_text(json.dumps(summary, indent=1, default=str))
    db.insert("reports", {"kind": "research", "period": summary["holdout"], "created_at": summary["created_at"],
                          "body": json.dumps(summary, default=str), "csv": None})
    db.kv_set("last_research_run", {"run_id": run_id, "at": summary["created_at"], "champion": summary["champion"]})


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--symbols", default="")
    ap.add_argument("--end", default="")
    a = ap.parse_args()
    syms = [x for x in a.symbols.split(",") if x] or None
    end = dt.date.fromisoformat(a.end) if a.end else None
    s = run(a.workers, syms, end)
    for v, r in s["reports"].items():
        o = r["oos"]
        print(f"{v:32s} {r['status']:9s} oosN={o.get('trades', 0):5d} expR={o.get('expectancy_r', 0) or 0:+.3f} "
              f"PF={o.get('profit_factor', 0) or 0:.2f} holm={r['holm_p']:.3f} holdN={r['holdout'].get('trades', 0)}")
    print("champion:", s["champion"])


if __name__ == "__main__":
    main()
