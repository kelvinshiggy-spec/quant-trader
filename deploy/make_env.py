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


def build(env: dict, overnight: bool, auto_approve: bool) -> str:
    key, sec, pw = env.get("ALPACA_PAPER_API_KEY", ""), env.get("ALPACA_PAPER_API_SECRET", ""), env.get("DASHBOARD_PASSWORD", "")
    missing = [n for n, v in (("ALPACA_PAPER_API_KEY", key), ("ALPACA_PAPER_API_SECRET", sec),
                              ("DASHBOARD_PASSWORD", pw)) if not v]
    if missing:
        raise SystemExit(f"missing environment variables: {', '.join(missing)}")
    if len(pw) < 10:
        raise SystemExit("DASHBOARD_PASSWORD must be at least 10 characters")
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
        "DASHBOARD_DOMAIN": "auto",  # replaced on the server with <ip-with-dashes>.sslip.io
        "DASHBOARD_USER": env.get("DASHBOARD_USER", "admin"),
        "DASHBOARD_PASSWORD_HASH": bcrypt.hashpw(pw.encode(), bcrypt.gensalt(12)).decode(),
        "SESSION_SECRET": secrets.token_hex(32),
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
    a = ap.parse_args()
    text = build(dict(os.environ), a.overnight, a.auto_approve)
    fd = os.open(a.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    # validate with the bot's own fail-closed loader (paper mode, key checks)
    from qt.config import load_settings
    env = dict(x.split("=", 1) for x in text.strip().splitlines())
    env["DASHBOARD_PASSWORD_HASH"] = env["DASHBOARD_PASSWORD_HASH"].strip("'")
    s = load_settings(env)
    print(f"wrote {a.out}: mode={s.mode} overnight={s.allow_overnight} auto_approve={s.auto_approve_validated}",
          file=sys.stderr)


if __name__ == "__main__":
    main()
