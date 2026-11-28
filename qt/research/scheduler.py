"""Research/maintenance process (separate container from the live engine).

* After each session close (+60 min): refresh history, run the full research
  pipeline (walk-forward, holdout, tournament update), rebuild nothing in the
  live engine; the engine just reads the updated strategy table.
* Owner-requested runs (dashboard "Run research") start at the next check.
* Daily SQLite backup (keeps 14) and forward-performance tracking.
Research never changes risk limits and never approves strategies.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import threading
import time
from pathlib import Path

from ..config import load_settings
from ..db import DB
from ..markettime import ET, now_utc, session

log = logging.getLogger("research-scheduler")


def backup(db: DB, data_dir: Path, keep: int = 14) -> Path:
    d = data_dir / "backups"
    d.mkdir(parents=True, exist_ok=True)
    out = d / f"qt_{now_utc().strftime('%Y%m%dT%H%M%SZ')}.db"
    db.backup(out)
    files = sorted(d.glob("qt_*.db"))
    for f in files[:-keep]:
        f.unlink()
    return out


def update_forward_metrics(db: DB) -> None:
    import json
    rows = db.query("SELECT strategy, COUNT(*) n, AVG(r_multiple) r, SUM(pnl) pnl FROM trades WHERE status='closed' "
                    "GROUP BY strategy")
    sh = {r["strategy"]: r for r in db.query("SELECT strategy, COUNT(*) n, AVG(r_multiple) r FROM shadow_trades "
                                             "WHERE status='closed' GROUP BY strategy")}
    for r in rows:
        db.execute("UPDATE strategy_status SET forward_metrics=? WHERE strategy=?",
                   (json.dumps({"paper_trades": r["n"], "paper_avg_r": r["r"], "paper_pnl": r["pnl"],
                                "shadow": sh.get(r["strategy"])}), r["strategy"]))
        # deterioration check: suspend from live selection if forward results collapse with a real sample
        if r["n"] >= 30 and (r["r"] or 0) < -0.15:
            db.execute("UPDATE strategy_status SET approved=0, status='suspended', reason=? WHERE strategy=?",
                       (f"forward paper deterioration: {r['n']} trades, avg R {r['r']:.3f}", r["strategy"]))
            db.event("warning", "strategy_suspended", f"{r['strategy']} suspended after forward deterioration")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    s = load_settings()
    db = DB(s.db_path)
    workers = int(os.environ.get("RESEARCH_WORKERS", "1"))
    symbols = [x for x in os.environ.get("RESEARCH_SYMBOLS", "").split(",") if x] or None
    from .runner import run
    while True:
        try:
            db.heartbeat("research", "idle")
            now = now_utc()
            today = now.astimezone(ET).date()
            sess = session(today)
            last = db.kv_get("research_last_day", "")
            req = db.kv_get("research_requested")
            # WEEKLY on a non-trading day (2026-11-28): a full run takes 15+ hours on the small server and starved
            # the engine and dashboard of CPU when it ran nightly. The forward books select strategies daily.
            last_d = dt.date.fromisoformat(last) if last else dt.date(2000, 1, 1)
            due_nightly = sess is None and (today - last_d).days >= 6
            if db.kv_get("backup_last_day", "") != today.isoformat() and (sess is None or now > sess[1]):
                p = backup(db, s.data_dir)
                db.kv_set("backup_last_day", today.isoformat())
                db.event("info", "backup", f"database backup {p.name}")
            if due_nightly or req:
                db.heartbeat("research", "running")
                db.kv_set("research_requested", None)
                t0 = time.time()
                # a run takes hours on the small server: keep beating so the health check (6 h limit for a
                # running job) does not restart it mid-run; a run past 48 h stops beating and is restarted
                done = threading.Event()

                def beat():
                    while not done.wait(300):
                        if time.time() - t0 < 48 * 3600:
                            db.heartbeat("research", "running", {"elapsed_min": round((time.time() - t0) / 60)})
                threading.Thread(target=beat, name="research-heartbeat", daemon=True).start()
                try:
                    summ = run(workers=workers, symbols=symbols, end=today - dt.timedelta(days=1))
                finally:
                    done.set()
                update_forward_metrics(db)
                db.kv_set("research_last_day", today.isoformat())
                db.event("info", "research", f"research run {summ['run_id']} finished in {time.time() - t0:.0f}s; "
                                             f"champion: {summ['champion']}")
        except Exception as e:  # noqa: BLE001
            log.exception("research cycle failed")
            db.event("error", "research", f"{type(e).__name__}: {e}")
            db.heartbeat("research", "error", str(e)[:200])
        time.sleep(300)


if __name__ == "__main__":
    main()
