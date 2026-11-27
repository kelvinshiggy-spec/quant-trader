"""SQLite persistence (WAL mode). One writer per table group:
engine writes trading state; research writes experiments; the API writes only
`commands` and `settings` (which the engine validates and applies).
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .markettime import now_utc

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS orders (
  client_order_id TEXT PRIMARY KEY, broker_order_id TEXT, trade_id TEXT, symbol TEXT, side TEXT, kind TEXT,
  type TEXT, qty REAL, notional REAL, limit_price REAL, stop_price REAL, status TEXT, filled_qty REAL DEFAULT 0,
  filled_avg_price REAL, submitted_at TEXT, updated_at TEXT, ack_latency_ms REAL, fill_latency_ms REAL,
  expected_price REAL, slippage_bps REAL, error TEXT, raw TEXT);
CREATE INDEX IF NOT EXISTS ix_orders_status ON orders(status);
CREATE TABLE IF NOT EXISTS trades (
  trade_id TEXT PRIMARY KEY, symbol TEXT, asset_class TEXT, strategy TEXT, strategy_version TEXT, status TEXT,
  entry_time TEXT, exit_time TEXT, entry_price REAL, exit_price REAL, qty REAL, notional REAL,
  stop_price REAL, initial_stop REAL, trail_stop REAL, target REAL, fees REAL DEFAULT 0, pnl REAL,
  r_multiple REAL, mfe REAL, mae REAL, planned_risk REAL, regime TEXT, score REAL, entry_reason TEXT,
  exit_reason TEXT, thesis TEXT, exit_plan TEXT, explanation TEXT, review TEXT, session TEXT,
  est_cost_bps REAL, actual_cost REAL);
CREATE TABLE IF NOT EXISTS opportunities (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, symbol TEXT, strategy TEXT, price REAL, change_pct REAL,
  relvol REAL, momentum REAL, regime TEXT, score REAL, est_cost_bps REAL, status TEXT, reason TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS ix_opp_ts ON opportunities(ts);
CREATE TABLE IF NOT EXISTS equity (ts TEXT PRIMARY KEY, equity REAL, cash REAL, settled REAL, unsettled REAL,
  positions_value REAL, realized REAL, unrealized REAL, source TEXT);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, level TEXT, kind TEXT,
  message TEXT, data TEXT);
CREATE INDEX IF NOT EXISTS ix_events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS commands (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, command TEXT, args TEXT,
  requested_by TEXT, status TEXT DEFAULT 'pending', result TEXT, processed_at TEXT);
CREATE TABLE IF NOT EXISTS heartbeats (component TEXT PRIMARY KEY, ts TEXT, status TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS experiments (
  id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, run_id TEXT, hypothesis TEXT, strategy TEXT,
  version TEXT, params TEXT, dataset TEXT, train_period TEXT, validation_period TEXT, test_period TEXT,
  results TEXT, limitations TEXT, decision TEXT, status TEXT);
CREATE TABLE IF NOT EXISTS strategy_status (
  strategy TEXT PRIMARY KEY, version TEXT, params TEXT, status TEXT, approved INTEGER DEFAULT 0,
  approved_at TEXT, champion INTEGER DEFAULT 0, oos_metrics TEXT, holdout_metrics TEXT, forward_metrics TEXT,
  allowed_regimes TEXT, reason TEXT, updated_at TEXT, history TEXT);
CREATE TABLE IF NOT EXISTS reports (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, period TEXT, created_at TEXT,
  body TEXT, csv TEXT);
CREATE TABLE IF NOT EXISTS shadow_trades (trade_id TEXT PRIMARY KEY, strategy TEXT, symbol TEXT, entry_time TEXT,
  exit_time TEXT, entry_price REAL, exit_price REAL, r_multiple REAL, pnl REAL, status TEXT, data TEXT);
"""


class DB:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.executescript(SCHEMA)

    @contextmanager
    def tx(self):
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def query(self, sql: str, params: tuple | dict = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def one(self, sql: str, params: tuple | dict = ()) -> dict | None:
        r = self.query(sql, params)
        return r[0] if r else None

    def upsert(self, table: str, row: dict, key: str) -> None:
        cols = list(row)
        sql = (f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))}) "
               f"ON CONFLICT({key}) DO UPDATE SET " + ",".join(f"{c}=excluded.{c}" for c in cols if c != key))
        self.execute(sql, tuple(_enc(row[c]) for c in cols))

    def insert(self, table: str, row: dict) -> int:
        cols = list(row)
        cur = self.execute(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                           tuple(_enc(row[c]) for c in cols))
        return cur.lastrowid

    # key/value state
    def kv_get(self, k: str, default: Any = None) -> Any:
        r = self.one("SELECT v FROM kv WHERE k=?", (k,))
        return default if r is None else json.loads(r["v"])

    def kv_set(self, k: str, v: Any) -> None:
        self.execute("INSERT INTO kv (k,v,updated_at) VALUES (?,?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v, "
                     "updated_at=excluded.updated_at", (k, json.dumps(v, default=str), now_utc().isoformat()))

    def event(self, level: str, kind: str, message: str, data: Any = None) -> None:
        self.insert("events", {"ts": now_utc().isoformat(), "level": level, "kind": kind, "message": message,
                               "data": data})

    def heartbeat(self, component: str, status: str = "ok", detail: Any = None) -> None:
        self.upsert("heartbeats", {"component": component, "ts": now_utc().isoformat(), "status": status,
                                   "detail": detail}, "component")

    def backup(self, dest: str | Path) -> None:
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            out = sqlite3.connect(str(dest))
            self._conn.backup(out)
            out.close()


def _enc(v):
    if isinstance(v, (dict, list, tuple)):
        return json.dumps(v, default=str)
    return v
