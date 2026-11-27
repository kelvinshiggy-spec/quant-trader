"""Daily / weekly / monthly reports built ONLY from recorded data.

Answers the owner's questions with rule-based assessments of the records
(never free-form guesses). If data is missing the report says so.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json

import numpy as np
import pandas as pd

from .db import DB
from .markettime import ET, PT, fmt_pt, now_utc


def _trades(db: DB, a: dt.date, b: dt.date) -> pd.DataFrame:
    rows = db.query("SELECT * FROM trades WHERE status='closed' AND session>=? AND session<=? ORDER BY exit_time",
                    (a.isoformat(), b.isoformat()))
    return pd.DataFrame(rows)


def _equity(db: DB, a: dt.date, b: dt.date) -> pd.DataFrame:
    rows = db.query("SELECT * FROM equity WHERE ts>=? AND ts<=? ORDER BY ts",
                    (dt.datetime.combine(a, dt.time(0), tzinfo=ET).astimezone(dt.timezone.utc).isoformat(),
                     dt.datetime.combine(b, dt.time(23, 59), tzinfo=ET).astimezone(dt.timezone.utc).isoformat()))
    return pd.DataFrame(rows)


def build_report(db: DB, kind: str, a: dt.date, b: dt.date, benchmark_pct: float | None = None) -> dict:
    t = _trades(db, a, b)
    eq = _equity(db, a, b)
    opp = pd.DataFrame(db.query("SELECT status, symbol, strategy, reason FROM opportunities WHERE ts>=? AND ts<=?",
                                (dt.datetime.combine(a, dt.time(0), tzinfo=ET).astimezone(dt.timezone.utc).isoformat(),
                                 dt.datetime.combine(b, dt.time(23, 59), tzinfo=ET).astimezone(dt.timezone.utc).isoformat())))
    events = pd.DataFrame(db.query("SELECT level, kind, message FROM events WHERE ts>=? AND ts<=?",
                                   (dt.datetime.combine(a, dt.time(0), tzinfo=ET).astimezone(dt.timezone.utc).isoformat(),
                                    dt.datetime.combine(b, dt.time(23, 59), tzinfo=ET).astimezone(dt.timezone.utc).isoformat())))
    rep: dict = {"kind": kind, "period": f"{a}..{b}", "generated_at": fmt_pt(now_utc()), "data_source": "Alpaca PAPER (simulated fills)"}
    if not eq.empty:
        start_bal, end_bal = float(eq["equity"].iloc[0]), float(eq["equity"].iloc[-1])
        peak = eq["equity"].cummax()
        rep.update(starting_balance=start_bal, ending_balance=end_bal, net_pnl=end_bal - start_bal,
                   return_pct=(end_bal / start_bal - 1) * 100 if start_bal else 0.0,
                   max_drawdown_pct=float(((peak - eq["equity"]) / peak).max() * 100),
                   session_high=float(eq["equity"].max()),
                   profit_given_back=float(eq["equity"].max() - end_bal))
    else:
        rep["note"] = "no equity snapshots recorded for this period (engine offline or market closed)"
    n = len(t)
    rep["total_trades"] = n
    if n:
        pnl = t["pnl"].astype(float)
        wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
        rep.update(winning_trades=int(len(wins)), losing_trades=int(len(losses)), win_rate=float(len(wins) / n),
                   avg_win=float(wins.mean()) if len(wins) else 0.0, avg_loss=float(losses.mean()) if len(losses) else 0.0,
                   profit_factor=float(wins.sum() / -losses.sum()) if len(losses) and losses.sum() < 0 else None,
                   fees=float(t["fees"].fillna(0).sum()))
        slip = pd.DataFrame(db.query("SELECT o.trade_id, o.slippage_bps, o.filled_qty, o.filled_avg_price FROM orders o "
                                     "JOIN trades t ON t.trade_id=o.trade_id WHERE t.session>=? AND t.session<=? "
                                     "AND o.slippage_bps IS NOT NULL", (a.isoformat(), b.isoformat())))
        if not slip.empty:
            rep["avg_slippage_bps"] = float(slip["slippage_bps"].mean())
            rep["est_slippage_cost"] = float((slip["slippage_bps"] / 1e4 * slip["filled_qty"] * slip["filled_avg_price"]).sum())
        by_sym = t.groupby("symbol")["pnl"].sum().sort_values(ascending=False)
        by_strat = t.groupby("strategy")["pnl"].sum().sort_values(ascending=False)
        rep["most_profitable_assets"] = by_sym.head(3).round(4).to_dict()
        rep["best_strategy"] = by_strat.index[0]
        rep["worst_strategy"] = by_strat.index[-1]
        rep["by_strategy"] = by_strat.round(4).to_dict()
    if not opp.empty:
        rep["assets_scanned"] = int(opp["symbol"].nunique())
        rep["qualifying_opportunities"] = int((opp["status"] == "EXECUTE").sum())
        rep["watch_opportunities"] = int((opp["status"] == "WATCH").sum())
        rep["rejected_opportunities"] = int((opp["status"] == "REJECT").sum())
    rep["executed_opportunities"] = n
    if not events.empty:
        rep["operational_errors"] = int(events["level"].isin(["error", "critical"]).sum())
        rep["critical_events"] = events[events["level"] == "critical"]["message"].head(10).tolist()
    hb = db.query("SELECT component, ts, status FROM heartbeats")
    rep["heartbeats"] = hb
    rep["benchmark_spy_pct"] = benchmark_pct
    rep["benchmark_cash_pct"] = 0.0
    rep["assessment"] = assess(rep, t)
    return rep


def assess(rep: dict, t: pd.DataFrame) -> dict:
    out = {}
    if rep.get("total_trades", 0) == 0:
        out["what_worked"] = "No trades executed; the system stayed in cash."
        out["what_did_not_work"] = "Nothing executed to evaluate."
        out["rules_followed"] = "No rule breaches recorded." if not rep.get("critical_events") else \
            f"{len(rep['critical_events'])} critical events need review."
        out["loss_causes"] = "No losses."
        out["missed_opportunities"] = (f"{rep.get('qualifying_opportunities', 0)} EXECUTE-class opportunities were "
                                       f"recorded; check the scanner log for ones blocked by capital or risk limits.")
        out["costs_excessive"] = "No costs incurred."
    else:
        bs = rep.get("by_strategy", {})
        out["what_worked"] = ", ".join(f"{k} ({v:+.2f})" for k, v in bs.items() if v > 0) or "No strategy was net positive."
        out["what_did_not_work"] = ", ".join(f"{k} ({v:+.2f})" for k, v in bs.items() if v <= 0) or "All strategies net positive."
        missing_stop = t[t["initial_stop"].isna()] if "initial_stop" in t else []
        out["rules_followed"] = ("Every trade had a predefined stop and exit plan." if len(missing_stop) == 0
                                 else f"{len(missing_stop)} trades lacked a recorded stop: investigate.")
        causes = []
        for _, r in t[t["pnl"] <= 0].iterrows():
            reason = r.get("exit_reason") or ""
            if "software" in reason or "error" in reason:
                causes.append("operational")
            elif (rep.get("avg_slippage_bps") or 0) > 3 * max(1.0, (r.get("est_cost_bps") or 0) / 2):
                causes.append("execution")
            else:
                causes.append("statistical variance within the validated rules")
        out["loss_causes"] = pd.Series(causes).value_counts().to_dict() if causes else "No losing trades."
        gross = float(t["pnl"].sum() + t["fees"].fillna(0).sum())
        cost = float(t["fees"].fillna(0).sum() + (rep.get("est_slippage_cost") or 0))
        out["costs_excessive"] = (f"Costs {cost:.4f} vs gross {gross:+.4f}: "
                                  + ("EXCESSIVE (more than half of gross profit)" if gross > 0 and cost > gross / 2
                                     else "acceptable" if gross > 0 else "gross result negative; costs added to loss"))
        out["missed_opportunities"] = (f"{rep.get('qualifying_opportunities', 0)} qualifying vs {rep['total_trades']} executed.")
    out["next_research"] = ("Accumulate forward trades before changing anything; review strategies whose forward "
                            "expectancy diverges from out-of-sample results.")
    return out


def to_csv(db: DB, a: dt.date, b: dt.date) -> str:
    t = _trades(db, a, b)
    buf = io.StringIO()
    cols = ["trade_id", "symbol", "strategy", "entry_time", "exit_time", "entry_price", "exit_price", "qty",
            "notional", "fees", "pnl", "r_multiple", "mfe", "mae", "entry_reason", "exit_reason", "regime", "score"]
    w = csv.writer(buf)
    w.writerow(cols)
    if not t.empty:
        for _, r in t.iterrows():
            w.writerow([r.get(c) for c in cols])
    return buf.getvalue()


def render_text(rep: dict) -> str:
    keys = ["period", "starting_balance", "ending_balance", "net_pnl", "return_pct", "max_drawdown_pct", "total_trades",
            "winning_trades", "losing_trades", "win_rate", "profit_factor", "fees", "avg_slippage_bps",
            "assets_scanned", "qualifying_opportunities", "executed_opportunities", "rejected_opportunities",
            "best_strategy", "worst_strategy", "profit_given_back", "operational_errors", "benchmark_spy_pct"]
    lines = [f"{rep['kind'].upper()} REPORT ({rep['data_source']})"]
    for k in keys:
        if k in rep and rep[k] is not None:
            v = rep[k]
            lines.append(f"{k.replace('_', ' ')}: {v:.4f}" if isinstance(v, float) else f"{k.replace('_', ' ')}: {v}")
    for k, v in rep.get("assessment", {}).items():
        lines.append(f"{k.replace('_', ' ')}: {v}")
    return "\n".join(lines)


def save_report(db: DB, rep: dict, csv_text: str) -> int:
    return db.insert("reports", {"kind": rep["kind"], "period": rep["period"], "created_at": now_utc().isoformat(),
                                 "body": json.dumps(rep, default=str), "csv": csv_text})
