"""Dashboard self-test, run on the server every 5 minutes by deploy/vm_health.sh (inside the api container).

Signs in the way a browser does (a signed session cookie made with the server's own SESSION_SECRET, so no
password is needed or stored) and loads the page, its script and stylesheet, and every data endpoint the
dashboard tabs call. Any failure here means a signed-in user would see an empty or broken tab.

Prints the failed checks and a summary line, exits 1 if any failed (--record also logs it as an event).
Standard library + itsdangerous only (fast to import).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request

from itsdangerous import TimestampSigner

BASE = os.environ.get("QT_SELFTEST_BASE", "http://127.0.0.1:8080")
PAGES = ["/", "/app.js", "/style.css"]
DATA = ["/api/overview", "/api/scanner", "/api/positions", "/api/guardian", "/api/tournament", "/api/research",
        "/api/trades", "/api/reports", "/api/system"]
# fields the Overview tab (the first thing shown after sign-in) reads directly
OVERVIEW_KEYS = ["mode", "data_label", "now_pt", "starting_capital", "equity", "equity_curve", "schedule_pt"]


def _get(path: str, cookie: str | None) -> tuple[int, bytes]:
    req = urllib.request.Request(BASE + path, headers={"X-Requested-With": "quant-trader"})
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def run() -> list[str]:
    secret = os.environ.get("SESSION_SECRET", "")
    user = os.environ.get("DASHBOARD_USER", "admin")
    cookie = "qt_session=" + TimestampSigner(secret).sign(user.encode()).decode()
    failures = []
    for path in PAGES:
        code, body = _get(path, None)
        if code != 200 or len(body) < 100:
            failures.append(f"{path}: HTTP {code}, {len(body)} bytes")
    for path in DATA:
        code, body = _get(path, cookie)
        if code != 200:
            failures.append(f"{path}: HTTP {code} {body[:200].decode('utf-8', 'replace')}")
            continue
        try:
            data = json.loads(body)
        except ValueError:
            failures.append(f"{path}: not JSON")
            continue
        if path == "/api/overview":
            missing = [k for k in OVERVIEW_KEYS if k not in data]
            if missing:
                failures.append(f"{path}: missing {', '.join(missing)}")
    return failures


def _record(message: str) -> None:
    """Log the failure in the bot's events table, so it shows on the System tab and in the health report."""
    data_dir = os.environ.get("QT_DATA_DIR", "data")
    try:
        con = sqlite3.connect(os.environ.get("QT_DB_PATH", os.path.join(data_dir, "qt.db")), timeout=10)
        con.execute("INSERT INTO events (ts, level, kind, message) VALUES (?, ?, ?, ?)",
                    (dt.datetime.now(dt.timezone.utc).isoformat(), "error", "dashboard_selftest", message[:1000]))
        con.commit()
        con.close()
    except sqlite3.Error as e:
        print("could not record the failure:", e)


def main(argv: list[str]) -> int:
    try:
        failures = run()
    except Exception as e:  # noqa: BLE001 - server not answering at all
        failures = [f"self-test could not run: {e}"]
    if failures:
        for f in failures:
            print("FAIL", f)
        print("dashboard_selftest: BROKEN")
        if "--record" in argv:
            _record("dashboard self-test failed: " + "; ".join(failures))
        return 1
    print(f"dashboard_selftest: OK ({len(PAGES) + len(DATA)} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
