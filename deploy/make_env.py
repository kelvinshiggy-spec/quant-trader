"""Build the production .env from environment variables, without printing any secret.

Reads: ALPACA_PAPER_API_KEY, ALPACA_PAPER_API_SECRET, DASHBOARD_PASSWORD (plain; only its bcrypt hash is
written), optional TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID, and the owner's execution choices.
Writes a mode-600 file and validates it with the same fail-closed loader the bot uses.

Usage: python -m deploy.make_env OUT_PATH [--overnight] [--auto-approve]
"""
from __future__ import annotations

import argparse
import os
import secrets
import sys

import bcrypt


def _password_hash(pw: str, prev_hash: str, change: bool) -> str:
    """The dashboard password survives redeploys. A redeploy from a session whose DASHBOARD_PASSWORD is out of date
    (env settings changed after the session started) used to silently reset the owner's password and lock them
    out, so the server's current hash is kept unless the change is asked for with CHANGE_DASHBOARD_PASSWORD=1."""
    if prev_hash and bcrypt.checkpw(pw.encode(), prev_hash.encode()):
        return prev_hash
    if prev_hash and not change:
        print("NOTE: DASHBOARD_PASSWORD here differs from the server's; keeping the server's password. "
              "Set CHANGE_DASHBOARD_PASSWORD=1 to change it.", file=sys.stderr)
        return prev_hash
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt(12)).decode()


def _ceiling(env: dict) -> str:
    import json
    from qt.config import RiskLimits
    lim = RiskLimits()
    if env.get("MAX_POSITIONS"):
        lim.max_positions = int(env["MAX_POSITIONS"])  # 2026-11-27: owner approved 8 (per-trade risk unchanged)
    return json.dumps(lim.to_dict(), separators=(",", ":"))


def build(env: dict, overnight: bool, auto_approve: bool, experimental: str = "", min_daily_trades: int = 0) -> str:
    key, sec, pw = env.get("ALPACA_PAPER_API_KEY", ""), env.get("ALPACA_PAPER_API_SECRET", ""), env.get("DASHBOARD_PASSWORD", "")
    missing = [n for n, v in (("ALPACA_PAPER_API_KEY", key), ("ALPACA_PAPER_API_SECRET", sec),
                              ("DASHBOARD_PASSWORD", pw)) if not v]
    if missing:
        raise SystemExit(f"missing environment variables: {', '.join(missing)}")
    if len(pw) < 10:
        raise SystemExit("DASHBOARD_PASSWORD must be at least 10 characters")
    # keep the session key and password hash of the running server when nothing changed, so a redeploy does
    # not sign the owner out of the dashboard (PREVIOUS_ENV_FILE: the .env currently in Secret Manager)
    prev = {}
    if env.get("PREVIOUS_ENV_FILE") and os.path.exists(env["PREVIOUS_ENV_FILE"]):
        prev = dict(x.split("=", 1) for x in open(env["PREVIOUS_ENV_FILE"]).read().splitlines() if "=" in x)
    prev_hash = prev.get("DASHBOARD_PASSWORD_HASH", "").strip("'")
    lines = {
        "TRADING_MODE": "paper",
        "ALPACA_PAPER_API_KEY": key,
        "ALPACA_PAPER_API_SECRET": sec,
        "ACCOUNT_TYPE": "cash",
        "ALPACA_DATA_FEED": "iex",
        "ALPACA_RESEARCH_FEED": "sip",
        "QT_DATA_DIR": "/app/data",
        "ALLOW_CRYPTO_EXECUTION": "false",
        "ALLOW_OVERNIGHT_HOLDS": "true" if overnight else "false",
        "AUTO_APPROVE_VALIDATED": "true" if auto_approve else "false",
        "EXPERIMENTAL_STRATEGIES": experimental,   # owner-approved, unvalidated, paper only
        "MIN_DAILY_TRADES": str(min_daily_trades),  # paper only; ignored in live mode
        "SWING_WIDE_UNIVERSE": env.get("SWING_WIDE_UNIVERSE", "0"),
        # owner-approved risk ceiling; only fields the owner changed differ from the spec defaults
        "QT_RISK_CEILING_JSON": _ceiling(env),
        "DASHBOARD_DOMAIN": "auto",  # replaced on the server with <ip-with-dashes>.sslip.io
        "DASHBOARD_USER": env.get("DASHBOARD_USER", "admin"),
        "DASHBOARD_PASSWORD_HASH": _password_hash(pw, prev_hash, env.get("CHANGE_DASHBOARD_PASSWORD") == "1"),
        "SESSION_SECRET": prev.get("SESSION_SECRET") or secrets.token_hex(32),
        "NOTIFY_LEVEL": env.get("NOTIFY_LEVEL", "important"),
        "TELEGRAM_BOT_TOKEN": env.get("TELEGRAM_BOT_TOKEN", ""),
        "TELEGRAM_CHAT_ID": env.get("TELEGRAM_CHAT_ID", ""),
        "RESEARCH_WORKERS": "1",
        "RESEARCH_MEM_LIMIT": "900m",
    }
    for k, v in lines.items():
        if "\n" in v:
            raise SystemExit(f"{k} contains a newline")
    # the bcrypt hash contains '$': single quotes make docker compose (and dotenv) read it literally
    out = []
    for k, v in lines.items():
        out.append(f"{k}='{v}'" if k == "DASHBOARD_PASSWORD_HASH" else f"{k}={v}")
    return "\n".join(out) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--overnight", action="store_true")
    ap.add_argument("--auto-approve", action="store_true")
    ap.add_argument("--experimental", default="", help="comma list of owner-approved unvalidated strategies (paper)")
    ap.add_argument("--min-daily-trades", type=int, default=0, help="paper only: force this many trades per day")
    a = ap.parse_args()
    text = build(dict(os.environ), a.overnight, a.auto_approve, a.experimental, a.min_daily_trades)
    fd = os.open(a.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    # validate with the bot's own fail-closed loader (paper mode, key checks)
    from qt.config import load_settings
    env = dict(x.split("=", 1) for x in text.strip().splitlines())
    env["DASHBOARD_PASSWORD_HASH"] = env["DASHBOARD_PASSWORD_HASH"].strip("'")
    s = load_settings(env)
    print(f"wrote {a.out}: mode={s.mode} overnight={s.allow_overnight} auto_approve={s.auto_approve_validated} "
          f"experimental={s.experimental_strategies} min_daily_trades={s.min_daily_trades}",
          file=sys.stderr)


if __name__ == "__main__":
    main()
