"""Alpaca implementation (alpaca-py 0.45, verified against the paper API 2026-11-27).

Facts verified by live probes on the paper account (see docs/ALPACA_NOTES.md):
* Fractional orders: market/limit/stop/stop_limit only, DAY only, order_class simple.
  Trailing-stop, bracket, OCO, OTO and GTC are REJECTED for fractional qty.
* client_order_id uniqueness is enforced by Alpaca ("client_order_id must be unique"),
  which makes resubmission with the same id idempotent.
* Crypto: market/limit/stop_limit, TIF gtc/ioc; no margin, no shorting.
"""
from __future__ import annotations

import datetime as dt
import logging
import time

import pandas as pd

from .base import Broker, BrokerError, OrderSpec, round_price, round_qty

log = logging.getLogger(__name__)


def _norm_order(o) -> dict:
    g = lambda k: getattr(o, k, None)  # noqa: E731
    f = lambda v: float(v) if v not in (None, "") else None  # noqa: E731
    return {
        "id": str(g("id")), "client_order_id": g("client_order_id"), "symbol": g("symbol"),
        "side": getattr(g("side"), "value", g("side")), "type": getattr(g("type"), "value", g("type")),
        "qty": f(g("qty")), "notional": f(g("notional")), "limit_price": f(g("limit_price")),
        "stop_price": f(g("stop_price")), "status": getattr(g("status"), "value", g("status")),
        "filled_qty": f(g("filled_qty")) or 0.0, "filled_avg_price": f(g("filled_avg_price")),
        "submitted_at": g("submitted_at"), "filled_at": g("filled_at"), "updated_at": g("updated_at"),
    }


def _classify(e: Exception) -> BrokerError:
    from alpaca.common.exceptions import APIError
    import requests
    if isinstance(e, APIError):
        code = getattr(e, "status_code", None)
        try:
            body_code = e.code
        except Exception:  # noqa: BLE001
            body_code = None
        uncertain = code is None or code >= 500 or code == 429
        return BrokerError(str(e)[:300], uncertain=uncertain, code=body_code or code)
    if isinstance(e, (requests.Timeout, requests.ConnectionError)):
        return BrokerError(f"network: {e}"[:300], uncertain=True)
    return BrokerError(f"{type(e).__name__}: {e}"[:300], uncertain=True)


class AlpacaBroker(Broker):
    def __init__(self, api_key: str, api_secret: str, paper: bool = True, data_feed: str = "iex"):
        from alpaca.data.historical import CryptoHistoricalDataClient, StockHistoricalDataClient
        from alpaca.trading.client import TradingClient
        if not paper:
            raise BrokerError("live trading is not enabled in this build without explicit owner authorization")
        self.paper = paper
        self.trading = TradingClient(api_key, api_secret, paper=paper)
        self.stock = StockHistoricalDataClient(api_key, api_secret)
        self.crypto = CryptoHistoricalDataClient(api_key, api_secret)
        self.feed = data_feed
        self._key, self._secret = api_key, api_secret
        self._asset_cache: dict[str, tuple[float, dict]] = {}

    # ---------- account ----------
    def get_account(self) -> dict:
        try:
            a = self.trading.get_account()
        except Exception as e:  # noqa: BLE001
            raise _classify(e) from e
        return {"equity": float(a.equity), "cash": float(a.cash), "buying_power": float(a.buying_power),
                "status": getattr(a.status, "value", str(a.status)), "trading_blocked": bool(a.trading_blocked),
                "account_blocked": bool(a.account_blocked), "crypto_status": str(a.crypto_status)}

    def get_clock(self) -> dict:
        try:
            c = self.trading.get_clock()
        except Exception as e:  # noqa: BLE001
            raise _classify(e) from e
        return {"timestamp": c.timestamp, "is_open": c.is_open, "next_open": c.next_open, "next_close": c.next_close}

    def get_calendar(self, start: dt.date, end: dt.date) -> list[dict]:
        from alpaca.trading.requests import GetCalendarRequest
        cal = self.trading.get_calendar(GetCalendarRequest(start=start, end=end))
        return [{"date": c.date, "open": c.open, "close": c.close} for c in cal]

    def list_positions(self) -> list[dict]:
        try:
            ps = self.trading.get_all_positions()
        except Exception as e:  # noqa: BLE001
            raise _classify(e) from e
        return [{"symbol": p.symbol, "qty": float(p.qty), "avg_entry_price": float(p.avg_entry_price),
                 "market_value": float(p.market_value or 0), "asset_class": getattr(p.asset_class, "value", str(p.asset_class)),
                 "current_price": float(p.current_price or 0)} for p in ps]

    # ---------- orders ----------
    def list_open_orders(self) -> list[dict]:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest
        try:
            os_ = self.trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500))
        except Exception as e:  # noqa: BLE001
            raise _classify(e) from e
        return [_norm_order(o) for o in os_]

    def get_order_by_client_id(self, cid: str) -> dict | None:
        from alpaca.common.exceptions import APIError
        try:
            return _norm_order(self.trading.get_order_by_client_id(cid))
        except APIError as e:
            if getattr(e, "status_code", None) == 404:
                return None
            raise _classify(e) from e
        except Exception as e:  # noqa: BLE001
            raise _classify(e) from e

    def submit_order(self, spec: OrderSpec) -> dict:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import (LimitOrderRequest, MarketOrderRequest, StopLimitOrderRequest,
                                             StopOrderRequest)
        side = OrderSide.BUY if spec.side == "buy" else OrderSide.SELL
        tif = TimeInForce(spec.time_in_force)
        common = dict(symbol=spec.symbol, side=side, time_in_force=tif, client_order_id=spec.client_order_id)
        if spec.qty is not None:
            common["qty"] = round_qty(spec.qty)
        else:
            common["notional"] = round(spec.notional, 2)
        if spec.type == "market":
            req = MarketOrderRequest(**common)
        elif spec.type == "limit":
            req = LimitOrderRequest(limit_price=round_price(spec.limit_price), **common)
        elif spec.type == "stop":
            req = StopOrderRequest(stop_price=round_price(spec.stop_price), **common)
        elif spec.type == "stop_limit":
            req = StopLimitOrderRequest(stop_price=round_price(spec.stop_price),
                                        limit_price=round_price(spec.limit_price), **common)
        else:
            raise BrokerError(f"unsupported order type {spec.type}")
        try:
            return _norm_order(self.trading.submit_order(req))
        except Exception as e:  # noqa: BLE001
            raise _classify(e) from e

    def cancel_order(self, broker_id: str) -> None:
        try:
            self.trading.cancel_order_by_id(broker_id)
        except Exception as e:  # noqa: BLE001
            raise _classify(e) from e

    def replace_order(self, broker_id: str, stop_price: float | None = None, qty: float | None = None) -> dict:
        from alpaca.trading.requests import ReplaceOrderRequest
        try:
            r = self.trading.replace_order_by_id(broker_id, ReplaceOrderRequest(
                stop_price=round_price(stop_price) if stop_price else None, qty=qty))
            return _norm_order(r)
        except Exception as e:  # noqa: BLE001
            raise _classify(e) from e

    def close_all_positions_broker(self) -> None:
        """NOT used automatically (it would also close positions this bot does not own)."""
        raise BrokerError("use the engine's close-all, which only closes ledger positions")

    # ---------- assets ----------
    def get_asset(self, symbol: str) -> dict:
        hit = self._asset_cache.get(symbol)
        if hit and time.time() - hit[0] < 3600:
            return hit[1]
        try:
            a = self.trading.get_asset(symbol)
        except Exception as e:  # noqa: BLE001
            raise _classify(e) from e
        d = {"symbol": a.symbol, "name": getattr(a, "name", "") or "", "tradable": bool(a.tradable), "fractionable": bool(a.fractionable),
             "status": getattr(a.status, "value", str(a.status)), "exchange": getattr(a.exchange, "value", str(a.exchange)),
             "asset_class": getattr(a.asset_class, "value", str(a.asset_class)), "shortable": bool(a.shortable),
             "min_order_size": float(a.min_order_size) if getattr(a, "min_order_size", None) else None,
             "min_trade_increment": float(a.min_trade_increment) if getattr(a, "min_trade_increment", None) else None,
             "price_increment": float(a.price_increment) if getattr(a, "price_increment", None) else None}
        self._asset_cache[symbol] = (time.time(), d)
        return d

    def list_assets(self, asset_class: str = "us_equity") -> list[dict]:
        from alpaca.trading.enums import AssetClass, AssetStatus
        from alpaca.trading.requests import GetAssetsRequest
        ac = AssetClass.CRYPTO if asset_class == "crypto" else AssetClass.US_EQUITY
        assets = self.trading.get_all_assets(GetAssetsRequest(status=AssetStatus.ACTIVE, asset_class=ac))
        return [{"symbol": a.symbol, "tradable": a.tradable, "fractionable": a.fractionable,
                 "exchange": getattr(a.exchange, "value", str(a.exchange)), "name": a.name} for a in assets]

    # ---------- market data ----------
    def latest_quotes(self, symbols: list[str]) -> dict[str, dict]:
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import CryptoLatestQuoteRequest, StockLatestQuoteRequest
        out = {}
        eq = [s for s in symbols if "/" not in s]
        cr = [s for s in symbols if "/" in s]
        try:
            if eq:
                q = self.stock.get_stock_latest_quote(StockLatestQuoteRequest(
                    symbol_or_symbols=eq, feed=DataFeed.IEX if self.feed == "iex" else DataFeed.SIP))
                for s, v in q.items():
                    out[s] = {"bid": float(v.bid_price or 0), "ask": float(v.ask_price or 0), "ts": v.timestamp}
            if cr:
                q = self.crypto.get_crypto_latest_quote(CryptoLatestQuoteRequest(symbol_or_symbols=cr))
                for s, v in q.items():
                    out[s] = {"bid": float(v.bid_price or 0), "ask": float(v.ask_price or 0), "ts": v.timestamp}
        except Exception as e:  # noqa: BLE001
            raise _classify(e) from e
        return out

    # ---------- options market data (read-only: the options book never sends orders) ----------
    def _opt(self):
        if getattr(self, "_option_client", None) is None:
            from alpaca.data.historical.option import OptionHistoricalDataClient
            self._option_client = OptionHistoricalDataClient(self._key, self._secret)
        return self._option_client

    def option_chain(self, underlying: str, exp_gte: dt.date, exp_lte: dt.date, kind: str = "call") -> list[dict]:
        from alpaca.data.requests import OptionChainRequest
        try:
            ch = self._opt().get_option_chain(OptionChainRequest(
                underlying_symbol=underlying, type=kind, expiration_date_gte=exp_gte, expiration_date_lte=exp_lte))
        except Exception as e:  # noqa: BLE001
            raise _classify(e) from e
        out = []
        for sym, v in ch.items():
            q = v.latest_quote
            out.append({"symbol": sym, "bid": float(q.bid_price or 0) if q else 0.0, "ask": float(q.ask_price or 0) if q else 0.0,
                        "ts": q.timestamp if q else None, "delta": float(v.greeks.delta) if v.greeks and v.greeks.delta is not None else None,
                        "iv": float(v.implied_volatility) if v.implied_volatility is not None else None})
        return out

    def option_quotes(self, symbols: list[str]) -> dict[str, dict]:
        from alpaca.data.requests import OptionSnapshotRequest
        if not symbols:
            return {}
        try:
            snap = self._opt().get_option_snapshot(OptionSnapshotRequest(symbol_or_symbols=symbols))
        except Exception as e:  # noqa: BLE001
            raise _classify(e) from e
        out = {}
        for sym, v in snap.items():
            q = v.latest_quote
            if q:
                out[sym] = {"bid": float(q.bid_price or 0), "ask": float(q.ask_price or 0), "ts": q.timestamp,
                            "delta": float(v.greeks.delta) if v.greeks and v.greeks.delta is not None else None}
        return out

    def snapshots(self, symbols: list[str]) -> dict[str, dict]:
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockSnapshotRequest
        out = {}
        for i in range(0, len(symbols), 200):
            chunk = symbols[i:i + 200]
            try:
                snap = self.stock.get_stock_snapshot(StockSnapshotRequest(
                    symbol_or_symbols=chunk, feed=DataFeed.IEX if self.feed == "iex" else DataFeed.SIP))
            except Exception as e:  # noqa: BLE001
                raise _classify(e) from e
            for s, v in snap.items():
                try:
                    db_, pdb, q, lt = v.daily_bar, v.previous_daily_bar, v.latest_quote, v.latest_trade
                    out[s] = {
                        "price": float(lt.price) if lt else None, "trade_ts": lt.timestamp if lt else None,
                        "bid": float(q.bid_price) if q else None, "ask": float(q.ask_price) if q else None,
                        "day_open": float(db_.open) if db_ else None, "day_high": float(db_.high) if db_ else None,
                        "day_low": float(db_.low) if db_ else None, "day_volume": float(db_.volume) if db_ else None,
                        "prev_close": float(pdb.close) if pdb else None, "prev_volume": float(pdb.volume) if pdb else None,
                        "minute_volume": float(v.minute_bar.volume) if v.minute_bar else None,
                    }
                except Exception:  # noqa: BLE001
                    continue
        return out

    def recent_bars(self, symbols: list[str], tf: str, start: dt.datetime) -> dict[str, pd.DataFrame]:
        from alpaca.data.enums import Adjustment, DataFeed
        from alpaca.data.requests import CryptoBarsRequest, StockBarsRequest
        from ..data.historical import _timeframe
        out = {}
        eq = [s for s in symbols if "/" not in s]
        cr = [s for s in symbols if "/" in s]
        try:
            frames = []
            if eq:
                frames.append(self.stock.get_stock_bars(StockBarsRequest(
                    symbol_or_symbols=eq, timeframe=_timeframe(tf), start=start, adjustment=Adjustment.RAW,
                    feed=DataFeed.IEX if self.feed == "iex" else DataFeed.SIP)).df)
            if cr:
                frames.append(self.crypto.get_crypto_bars(CryptoBarsRequest(
                    symbol_or_symbols=cr, timeframe=_timeframe(tf), start=start)).df)
        except Exception as e:  # noqa: BLE001
            raise _classify(e) from e
        for df in frames:
            if df is None or df.empty:
                continue
            for s in df.index.get_level_values(0).unique():
                d = df.xs(s, level=0)[["open", "high", "low", "close", "volume"]].copy()
                d.index = pd.DatetimeIndex(d.index).tz_convert("UTC")
                out[s] = d.sort_index()
        return out
