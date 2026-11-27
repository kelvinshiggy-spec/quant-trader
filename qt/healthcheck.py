"""Container health checks (exit 0 = healthy).

Standard library only, on purpose: on a 2-vCPU shared-core server importing pandas can take longer than the
check's timeout, which made Docker mark healthy containers unhealthy and autoheal restart them in a loop.
"""
import datetime as dt
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path


def _db_path() -> str:
    data_dir = Path(os.environ.get("QT_DATA_DIR", "data"))
    return os.environ.get("QT_DB_PATH", str(data_dir / "qt.db"))


def main(component: str) -> int:
    if component == "api":
        try:
            urllib.request.urlopen("http://127.0.0.1:8080/api/health", timeout=20)
        except urllib.error.HTTPError:
            return 0  # server answered (503 just means the engine is unhealthy)
        except Exception:  # noqa: BLE001
            return 1
        return 0
    try:
        con = sqlite3.connect(f"file:{_db_path()}?mode=ro", uri=True, timeout=10)
        hb = con.execute("SELECT ts, status FROM heartbeats WHERE component=?", (component,)).fetchone()
        con.close()
    except sqlite3.Error:
        return 1
    if not hb:
        return 1
    ts, status = hb
    t = dt.datetime.fromisoformat(ts)
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    age = (dt.datetime.now(dt.timezone.utc) - t).total_seconds()
    limit = 120 if component == "engine" else 3 * 3600
    if component == "research" and status == "running":
        limit = 6 * 3600
    return 0 if age < limit else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
