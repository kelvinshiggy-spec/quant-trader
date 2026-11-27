"""Optional Telegram / email notifications with levels:
all (every trade), important (stops, suspensions, errors, risk, reports), daily (report only), off.
Disabled unless configured. Never includes credentials. Rate-limited."""
from __future__ import annotations

import logging
import smtplib
import time
from email.message import EmailMessage

log = logging.getLogger("notify")

IMPORTANT = {"stop_loss", "strategy_suspended", "error", "connection_lost", "stale_data", "risk_threshold",
             "emergency", "unexpected_position", "daily_report", "critical"}
TRADE = {"trade_open", "trade_close"}


class Notifier:
    def __init__(self, settings, db=None, sender=None):
        self.s, self.db = settings, db
        self._sender = sender
        self._recent: dict[str, float] = {}

    def wants(self, kind: str) -> bool:
        lvl = self.s.notify_level
        if lvl == "off":
            return False
        if lvl == "daily":
            return kind == "daily_report"
        if lvl == "important":
            return kind in IMPORTANT
        return kind in IMPORTANT | TRADE

    def __call__(self, kind: str, text: str) -> None:
        if self.db is not None:
            self.db.event("info", f"notify:{kind}", text)
        if not self.wants(kind):
            return
        key = f"{kind}:{text[:60]}"
        if time.time() - self._recent.get(key, 0) < 300:  # de-duplicate bursts
            return
        self._recent[key] = time.time()
        msg = f"[quant-trader {self.s.mode.upper()}] {text}"
        try:
            if self._sender:
                self._sender(msg)
            if self.s.telegram_token and self.s.telegram_chat_id:
                import httpx
                httpx.post(f"https://api.telegram.org/bot{self.s.telegram_token}/sendMessage",
                           data={"chat_id": self.s.telegram_chat_id, "text": msg}, timeout=10)
            if self.s.smtp_host and self.s.email_to:
                em = EmailMessage()
                em["Subject"] = msg[:80]
                em["From"] = self.s.smtp_user or "quant-trader@localhost"
                em["To"] = self.s.email_to
                em.set_content(msg)
                with smtplib.SMTP(self.s.smtp_host, self.s.smtp_port, timeout=15) as srv:
                    srv.starttls()
                    if self.s.smtp_user:
                        srv.login(self.s.smtp_user, self.s.smtp_password)
                    srv.send_message(em)
        except Exception as e:  # noqa: BLE001
            log.warning("notification failed: %s", type(e).__name__)
