"""Container health checks (exit 0 = healthy)."""
import datetime as dt
import sys
import urllib.request

from .config import load_settings
from .db import DB


def main(component: str) -> int:
    if component == "api":
        try:
            urllib.request.urlopen("http://127.0.0.1:8080/api/health", timeout=5)
        except urllib.error.HTTPError:
            return 0  # server answered (503 just means the engine is unhealthy)
        except Exception:  # noqa: BLE001
            return 1
        return 0
    s = load_settings(require_credentials=False)
    hb = DB(s.db_path).one("SELECT ts, status FROM heartbeats WHERE component=?", (component,))
    if not hb:
        return 1
    age = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(hb["ts"])).total_seconds()
    limit = 120 if component == "engine" else 3 * 3600
    if component == "research" and hb["status"] == "running":
        limit = 6 * 3600
    return 0 if age < limit else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
