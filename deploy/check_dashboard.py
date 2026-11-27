"""Pre-deploy dashboard check (run by deploy/redeploy.sh; a failure stops the deploy).

Starts the dashboard locally on a throwaway copy of a database (an empty one by default, or --db PATH),
then 1) runs the server self-test (every tab's data loads) and 2) if Node + Playwright are available, signs in
from a phone-sized headless browser and checks that every tab renders (deploy/check_dashboard.js).
"""
from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import bcrypt

HERE = Path(__file__).resolve().parent


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", help="database to test against (a copy is used; default: a new empty one)")
    args = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="qt-dash-"))
    try:
        db = tmp / "qt.db"
        if args.db:
            shutil.copy(args.db, db)
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        pw = "check-" + os.urandom(6).hex()
        os.environ.update({"QT_DATA_DIR": str(tmp), "QT_DB_PATH": str(db), "SESSION_SECRET": os.urandom(24).hex(),
                           "DASHBOARD_USER": "admin",
                           "DASHBOARD_PASSWORD_HASH": bcrypt.hashpw(pw.encode(), bcrypt.gensalt(4)).decode(),
                           "QT_SELFTEST_BASE": f"http://127.0.0.1:{port}"})
        import uvicorn

        from qt.api import selftest
        from qt.api.app import create_app
        from qt.db import DB

        DB(db)  # creates the schema for an empty database
        server = uvicorn.Server(uvicorn.Config(create_app(), host="127.0.0.1", port=port, log_level="warning"))
        threading.Thread(target=server.run, daemon=True).start()
        for _ in range(100):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/app.js", timeout=2)
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.1)
        ok = selftest.main([]) == 0
        if shutil.which("node"):
            r = subprocess.run(["node", str(HERE / "check_dashboard.js"), f"http://127.0.0.1:{port}", "admin", pw],
                               capture_output=True, text=True, timeout=180)
            out = (r.stdout + r.stderr).strip()
            if "Cannot find module 'playwright'" in out:
                print("dashboard_render_check: SKIPPED (Playwright not installed here)")
            else:
                print(out)
                ok = ok and r.returncode == 0
        else:
            print("dashboard_render_check: SKIPPED (Node not installed here)")
        server.should_exit = True
        return 0 if ok else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
