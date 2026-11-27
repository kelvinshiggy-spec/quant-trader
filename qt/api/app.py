"""Dashboard API (FastAPI). Reads the shared SQLite DB; never calls the broker.

Security: every /api route except /api/health and /api/login needs a signed,
HttpOnly, SameSite=Strict session cookie. State-changing requests also need
the X-Requested-With header (CSRF defence) and sensitive commands need a typed
confirmation phrase. Live trading cannot be enabled from here at all.
"""
from __future__ import annotations

import datetime as dt
import json
import time
from pathlib import Path

import bcrypt
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from itsdangerous import BadSignature, SignatureExpired, TimestampSigner

from ..config import RiskLimits, check_within_ceiling, load_settings
from ..db import DB
from ..markettime import ET, fmt_pt, now_utc, pt_schedule_for

SESSION_COOKIE = "qt_session"
SESSION_MAX_AGE = 12 * 3600
CONFIRM = {"close_all": "CLOSE ALL", "emergency_shutdown": "EMERGENCY", "reset_drawdown_halt": "RESET",
           "set_risk_limits": "CHANGE RISK"}
ALLOWED_COMMANDS = {"pause", "resume", "start_paper", "emergency_shutdown", "clear_emergency", "close_all",
                    "cancel_pending", "set_risk_limits", "approve_strategy", "disable_strategy", "reset_drawdown_halt",
                    "run_backtest", "ignore_symbol"}
STATIC = Path(__file__).resolve().parent.parent / "dashboard"


def create_app(settings=None, db: DB | None = None) -> FastAPI:
    s = settings or load_settings(require_credentials=False)
    if not s.session_secret or len(s.session_secret) < 32:
        raise RuntimeError("SESSION_SECRET (>= 32 chars) is required for the dashboard")
    if not s.dashboard_password_hash.startswith("$2"):
        raise RuntimeError("DASHBOARD_PASSWORD_HASH must be a bcrypt hash (see README)")
    db = db or DB(s.db_path)
    signer = TimestampSigner(s.session_secret)
    app = FastAPI(title="quant-trader", docs_url=None, redoc_url=None, openapi_url=None)
    attempts: dict[str, list[float]] = {}

    def auth(request: Request) -> str:
        tok = request.cookies.get(SESSION_COOKIE)
        if not tok:
            raise HTTPException(401, "login required")
        try:
            return signer.unsign(tok, max_age=SESSION_MAX_AGE).decode()
        except (BadSignature, SignatureExpired):
            raise HTTPException(401, "session expired")

    def csrf(request: Request):
        if request.headers.get("x-requested-with") != "quant-trader":
            raise HTTPException(403, "missing CSRF header")

    @app.middleware("http")
    async def headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Content-Security-Policy"] = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                                                   "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
        resp.headers["Cache-Control"] = "no-store"
        return resp

    # ---------------- public ----------------
    @app.get("/api/health")
    def health():
        hb = db.one("SELECT ts, status FROM heartbeats WHERE component='engine'")
        age = None
        if hb:
            age = (now_utc() - dt.datetime.fromisoformat(hb["ts"])).total_seconds()
        ok = hb is not None and age is not None and age < 120 and hb["status"] == "ok"
        return JSONResponse({"engine_ok": ok, "heartbeat_age_s": age}, status_code=200 if ok else 503)

    @app.post("/api/login")
    async def login(request: Request, response: Response):
        ip = request.client.host if request.client else "?"
        now = time.time()
        attempts[ip] = [t for t in attempts.get(ip, []) if now - t < 900]
        if len(attempts[ip]) >= 8:
            raise HTTPException(429, "too many attempts; wait 15 minutes")
        body = await request.json()
        user, pw = str(body.get("username", "")), str(body.get("password", ""))
        user = user.strip().lower() if user.strip().lower() == s.dashboard_user.lower() else user
        h = s.dashboard_password_hash.encode()
        # phones often add a trailing space when pasting or autocompleting; accept the trimmed password too
        ok = user == s.dashboard_user and (bcrypt.checkpw(pw.encode(), h) or (pw != pw.strip() and bcrypt.checkpw(pw.strip().encode(), h)))
        if not ok:
            attempts[ip].append(now)
            db.event("warning", "auth", f"failed dashboard login from {ip}")
            raise HTTPException(401, "invalid credentials")
        tok = signer.sign(user.encode()).decode()
        secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
        response.set_cookie(SESSION_COOKIE, tok, max_age=SESSION_MAX_AGE, httponly=True, samesite="strict", secure=secure)
        db.event("info", "auth", f"dashboard login from {ip}")
        return {"ok": True}

    @app.post("/api/logout")
    def logout(response: Response):
        response.delete_cookie(SESSION_COOKIE)
        return {"ok": True}

    # ---------------- data ----------------
    def _j(v):
        if isinstance(v, str):
            try:
                return json.loads(v)
            except Exception:  # noqa: BLE001
                return v
        return v

    @app.get("/api/overview")
    def overview(user=Depends(auth)):
        led = db.kv_get("ledger") or {}
        g = db.kv_get("guardian_status") or {}
        marks = db.kv_get("marks") or {}
        live = db.kv_get("live") or {}
        eq = db.query("SELECT ts, equity FROM equity ORDER BY ts")
        start = led.get("starting_cash", s.risk_ceiling.starting_capital)
        cur = eq[-1]["equity"] if eq else start
        prices = live.get("prices") or {}
        positions = led.get("positions", {})
        unreal = sum(p["qty"] * (prices.get(sym) or p["avg_price"]) - p["cost_basis"] for sym, p in positions.items())
        today = now_utc().astimezone(ET).date()

        def pnl_since(d0: dt.date):
            rows = db.query("SELECT SUM(pnl) p FROM trades WHERE status='closed' AND session>=?", (d0.isoformat(),))
            return rows[0]["p"] or 0.0

        unsettled = sum(u["amount"] for u in led.get("unsettled", []))
        return {
            "mode": s.mode, "data_label": "PAPER trading: simulated fills on Alpaca's paper venue; virtual $%.0f ledger" % start,
            "starting_capital": start, "equity": cur, "cash": led.get("settled_cash", start) + unsettled,
            "settled": led.get("settled_cash", start), "unsettled": unsettled,
            "buying_power": max(0.0, led.get("settled_cash", start) - sum((led.get("reservations") or {}).values())),
            "realized": led.get("realized_pnl", 0.0), "unrealized": unreal,
            "today": pnl_since(today), "week": pnl_since(today - dt.timedelta(days=today.weekday())),
            "month": pnl_since(today.replace(day=1)),
            "total_return_pct": (cur / start - 1) * 100 if start else 0,
            "drawdown_pct": g.get("drawdown_pct"), "peak_equity": marks.get("peak", start),
            "equity_curve": eq[-2000:], "fees": led.get("fees_paid", 0.0),
            "schedule_pt": {k: (v.strftime("%a %b %d %I:%M %p PT") if hasattr(v, "strftime") else v)
                            for k, v in pt_schedule_for(today).items()},
            "now_pt": fmt_pt(now_utc()),
        }

    @app.get("/api/scanner")
    def scanner(user=Depends(auth)):
        last = db.kv_get("last_scan_summary") or {}
        rows = []
        if last.get("ts"):
            rows = db.query("SELECT * FROM opportunities WHERE ts=? ORDER BY CASE status WHEN 'EXECUTE' THEN 0 "
                            "WHEN 'WATCH' THEN 1 ELSE 2 END, score DESC LIMIT 300", (last["ts"],))
        for r in rows:
            r["detail"] = _j(r["detail"])
        return {"summary": last, "stage1": db.kv_get("stage1") or {}, "opportunities": rows,
                "note": "Score is a 0-100 ranking index, NOT a win probability (uncalibrated until enough forward trades)."}

    @app.get("/api/positions")
    def positions(user=Depends(auth)):
        live = db.kv_get("live") or {}
        rows = db.query("SELECT * FROM trades WHERE status IN ('pending_entry','open','exiting') ORDER BY entry_time")
        now = now_utc()
        for r in rows:
            px = (live.get("prices") or {}).get(r["symbol"])
            r["current_price"] = px
            r["pnl_now"] = (px - r["entry_price"]) * r["qty"] - (r["fees"] or 0) if px and r["entry_price"] else None
            r["holding_minutes"] = (now - dt.datetime.fromisoformat(r["entry_time"])).total_seconds() / 60 if r["entry_time"] else None
            r["explanation"] = _j(r["explanation"])
        return {"positions": rows, "price_source": "IEX real-time quote midpoint", "as_of": live.get("ts")}

    @app.get("/api/guardian")
    def guardian(user=Depends(auth)):
        lim = db.kv_get("risk_limits") or s.risk_ceiling.to_dict()
        return {"status": db.kv_get("guardian_status") or {}, "limits": lim, "ceiling": s.risk_ceiling.to_dict(),
                "flags": db.kv_get("flags") or {}, "marks": db.kv_get("marks") or {},
                "principal_note": ("With a $100 floor on a $100 account there is NO loss cushion: Principal Protection "
                                   "would block all risk-taking until profits exist. Floors are not guarantees; gaps, "
                                   "slippage and outages can breach them.")}

    @app.get("/api/tournament")
    def tournament(user=Depends(auth)):
        rows = db.query("SELECT * FROM strategy_status ORDER BY champion DESC, status, strategy")
        for r in rows:
            for k in ("oos_metrics", "holdout_metrics", "forward_metrics", "allowed_regimes", "history", "params"):
                r[k] = _j(r[k])
            if r["strategy"] in s.experimental_strategies and r["status"] in ("shadow", "validated"):
                r["status"] = "EXPERIMENTAL (unvalidated) - paper trading"
                r["approved"] = 1
        shadow = db.query("SELECT strategy, COUNT(*) n, AVG(r_multiple) avg_r FROM shadow_trades WHERE status='closed' "
                          "GROUP BY strategy")
        live = db.query("SELECT strategy, COUNT(*) n, SUM(pnl) pnl, AVG(r_multiple) avg_r FROM trades WHERE "
                        "status='closed' GROUP BY strategy")
        return {"strategies": rows, "shadow_forward": shadow, "paper_forward": live,
                "last_run": db.kv_get("last_research_run")}

    @app.get("/api/research")
    def research(user=Depends(auth)):
        ex = db.query("SELECT id, created_at, run_id, strategy, version, params, train_period, test_period, decision, "
                      "status FROM experiments ORDER BY id DESC LIMIT 400")
        runs = db.query("SELECT id, created_at, period FROM reports WHERE kind='research' ORDER BY id DESC LIMIT 20")
        return {"experiments": ex, "runs": runs, "requested": db.kv_get("research_requested"),
                "pending_approvals": db.query("SELECT strategy FROM strategy_status WHERE status='validated' AND approved=0")}

    @app.get("/api/experiment/{eid}")
    def experiment(eid: int, user=Depends(auth)):
        r = db.one("SELECT * FROM experiments WHERE id=?", (eid,))
        if not r:
            raise HTTPException(404)
        r["results"] = _j(r["results"])
        return r

    @app.get("/api/trades")
    def trades(limit: int = 500, user=Depends(auth)):
        rows = db.query("SELECT * FROM trades WHERE status IN ('closed','entry_failed') ORDER BY entry_time DESC LIMIT ?",
                        (min(limit, 5000),))
        for r in rows:
            r["explanation"] = _j(r["explanation"])
        return {"trades": rows}

    @app.get("/api/system")
    def system(user=Depends(auth)):
        hb = db.query("SELECT * FROM heartbeats")
        live = db.kv_get("live") or {}
        ev = db.query("SELECT ts, level, kind, message FROM events ORDER BY id DESC LIMIT 100")
        pend = db.query("SELECT client_order_id, symbol, side, kind, type, status, submitted_at FROM orders WHERE status "
                        "NOT IN ('filled','canceled','expired','rejected','replaced','done_for_day')")
        errors = db.query("SELECT COUNT(*) c FROM events WHERE level IN ('error','critical') AND ts>?",
                          ((now_utc() - dt.timedelta(days=1)).isoformat(),))[0]["c"]
        lat = db.one("SELECT AVG(ack_latency_ms) ack, AVG(fill_latency_ms) fill, AVG(slippage_bps) slip FROM orders "
                     "WHERE submitted_at>?", ((now_utc() - dt.timedelta(days=7)).isoformat(),))
        return {"mode": s.mode, "heartbeats": hb, "live": live, "events": ev, "pending_orders": pend,
                "errors_24h": errors, "latency": lat, "data_feeds": {"live": "IEX real-time (free plan, single exchange)",
                                                                       "research": "SIP historical (>15 min delayed)"},
                "now_pt": fmt_pt(now_utc())}

    @app.get("/api/reports")
    def reports(user=Depends(auth)):
        rows = db.query("SELECT id, kind, period, created_at FROM reports ORDER BY id DESC LIMIT 100")
        return {"reports": rows}

    @app.get("/api/report/{rid}")
    def report(rid: int, user=Depends(auth)):
        r = db.one("SELECT * FROM reports WHERE id=?", (rid,))
        if not r:
            raise HTTPException(404)
        r["body"] = _j(r["body"])
        return r

    @app.get("/api/download/trades.csv")
    def dl_trades(user=Depends(auth)):
        from ..reporting import to_csv
        return PlainTextResponse(to_csv(db, dt.date(2000, 1, 1), dt.date(2100, 1, 1)), media_type="text/csv",
                                 headers={"Content-Disposition": "attachment; filename=trades.csv"})

    @app.get("/api/download/report/{rid}")
    def dl_report(rid: int, fmt: str = "json", user=Depends(auth)):
        r = db.one("SELECT * FROM reports WHERE id=?", (rid,))
        if not r:
            raise HTTPException(404)
        if fmt == "csv":
            return PlainTextResponse(r["csv"] or "", media_type="text/csv",
                                     headers={"Content-Disposition": f"attachment; filename=report_{rid}.csv"})
        return PlainTextResponse(r["body"], media_type="application/json",
                                 headers={"Content-Disposition": f"attachment; filename=report_{rid}.json"})

    # ---------------- control ----------------
    @app.post("/api/command")
    async def command(request: Request, user=Depends(auth), _=Depends(csrf)):
        body = await request.json()
        cmd = body.get("command")
        args = body.get("args") or {}
        if cmd not in ALLOWED_COMMANDS:
            raise HTTPException(400, "unknown command")
        need = CONFIRM.get(cmd)
        if cmd == "approve_strategy":
            need = f"APPROVE {args.get('strategy', '')}"
        if need and body.get("confirm") != need:
            raise HTTPException(400, f"type '{need}' to confirm")
        if cmd == "set_risk_limits":
            cur = RiskLimits.from_dict({**(db.kv_get("risk_limits") or s.risk_ceiling.to_dict()), **args})
            errs = check_within_ceiling(cur, s.risk_ceiling)
            if errs:
                raise HTTPException(400, "exceeds the approved ceiling: " + "; ".join(errs))
        if cmd == "run_backtest":
            db.kv_set("research_requested", {"at": now_utc().isoformat(), "by": user})
            return {"ok": True, "queued": "research container will run at its next check"}
        cid = db.insert("commands", {"ts": now_utc().isoformat(), "command": cmd, "args": args, "requested_by": user})
        return {"ok": True, "command_id": cid, "note": "the engine applies commands within ~15 seconds"}

    @app.get("/api/command/{cid}")
    def command_status(cid: int, user=Depends(auth)):
        return db.one("SELECT * FROM commands WHERE id=?", (cid,)) or {}

    client_reports: list[float] = []

    @app.post("/api/client_error")
    async def client_error(request: Request, user=Depends(auth), _=Depends(csrf)):
        """The dashboard reports script errors and blank tabs seen on the owner's device (shown on System tab)."""
        now = time.time()
        client_reports[:] = [t for t in client_reports if now - t < 3600]
        if len(client_reports) >= 30:  # at most 30 reports an hour
            return {"ok": False}
        client_reports.append(now)
        body = await request.json()
        ua = request.headers.get("user-agent", "")[:160]
        db.event("warning", "dashboard_client", f"{str(body.get('message', ''))[:400]} | tab={str(body.get('tab', ''))[:20]} | {ua}")
        return {"ok": True}

    # ---------------- static dashboard ----------------
    @app.get("/", response_class=HTMLResponse)
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/app.js")
    def appjs():
        return FileResponse(STATIC / "app.js", media_type="application/javascript")

    @app.get("/style.css")
    def css():
        return FileResponse(STATIC / "style.css", media_type="text/css")

    return app


def main():
    import uvicorn
    uvicorn.run(create_app(), host="0.0.0.0", port=8080, proxy_headers=True, forwarded_allow_ips="*")


if __name__ == "__main__":
    main()
