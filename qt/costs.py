"""Transaction cost model used identically by backtests and pre-trade estimates.

Equities (Alpaca): $0 commission. Regulatory fees on SELLS are passed through:
SEC Section 31 fee (rate configurable; set conservatively) and FINRA TAF per
share (capped). Spread and slippage are estimated because the free data plan
does not provide full historical quotes; estimates are deliberately
conservative and are stress-tested at 2x and 3x in research.

Crypto (Alpaca, lowest volume tier, verified 2026-11-27 in docs):
maker 0.15%, taker 0.25%. We assume taker on every fill.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CostModel:
    slippage_bps_equity: float = 2.0
    slippage_bps_crypto: float = 5.0
    sec_fee_rate: float = 27.80e-6      # $ per $ of sale proceeds (conservative)
    taf_per_share: float = 0.000195
    taf_max: float = 9.79
    crypto_taker_bps: float = 25.0
    crypto_maker_bps: float = 15.0
    stress: float = 1.0                 # multiplies spread + slippage + crypto fees

    def spread_bps(self, asset_class: str, avg_dollar_volume: float | None, symbol: str = "") -> float:
        """Estimated FULL quoted spread in bps from liquidity tier."""
        if asset_class == "crypto":
            base = 4.0 if symbol in ("BTC/USD", "ETH/USD") else 12.0
        else:
            adv = avg_dollar_volume or 0.0
            if adv >= 5e9:      # SPY/QQQ/NVDA-class: 1-cent spreads on $100+ prices
                base = 0.5
            elif adv >= 1e9:
                base = 1.5
            elif adv >= 2e8:
                base = 3.0
            elif adv >= 5e7:
                base = 6.0
            elif adv >= 1e7:
                base = 15.0
            else:
                base = 40.0
        return base * self.stress

    def _impact_bps(self, asset_class: str, spread_bps: float) -> float:
        slip = self.slippage_bps_crypto if asset_class == "crypto" else self.slippage_bps_equity
        return spread_bps / 2.0 + slip * self.stress

    def buy_price(self, ref: float, asset_class: str, spread_bps: float) -> float:
        return ref * (1 + self._impact_bps(asset_class, spread_bps) / 1e4)

    def sell_price(self, ref: float, asset_class: str, spread_bps: float) -> float:
        return ref * (1 - self._impact_bps(asset_class, spread_bps) / 1e4)

    def stop_sell_price(self, stop: float, bar_open: float, asset_class: str, spread_bps: float) -> float:
        """Stop orders become market orders: fill at the worse of stop and the open (gaps)."""
        ref = min(stop, bar_open)
        return self.sell_price(ref, asset_class, spread_bps)

    def fee(self, side: str, qty: float, notional: float, asset_class: str) -> float:
        if asset_class == "crypto":
            return notional * self.crypto_taker_bps * self.stress / 1e4
        if side == "sell":
            sec = notional * self.sec_fee_rate
            taf = min(self.taf_max, qty * self.taf_per_share)
            return sec + taf
        return 0.0

    def round_trip_bps(self, asset_class: str, spread_bps: float) -> float:
        """Estimated total round-trip cost in bps of notional (for pre-trade checks)."""
        impact = 2 * self._impact_bps(asset_class, spread_bps)
        if asset_class == "crypto":
            return impact + 2 * self.crypto_taker_bps * self.stress
        return impact + self.sec_fee_rate * 1e4
