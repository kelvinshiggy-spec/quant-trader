"""Opportunity scoring (0-100) and EXECUTE / WATCH / REJECT classification.

The score is a weighted RANKING index, not a probability of winning. Its
weights are initial design choices; the research engine reports realized
forward R by score bucket so the scale can be calibrated once enough forward
trades exist. Until then the dashboard labels it "uncalibrated".
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

WEIGHTS = {"edge": 30, "reliability": 15, "signal": 15, "regime": 10, "liquidity": 10, "cost": 10, "freshness": 5,
           "diversification": 5}
EXECUTE_MIN_SCORE = 50.0


@dataclass
class Opportunity:
    symbol: str
    strategy: str
    price: float
    change_pct: float
    relvol: float | None
    momentum: float | None
    regime: str
    stop: float
    target: float | None
    atr: float
    spread_bps: float
    est_cost_bps: float
    data_age_s: float
    signal: bool
    strength: float
    strategy_status: str          # validated | experimental (owner-approved, unvalidated) | shadow | rejected | suspended
    strategy_approved: bool
    oos_expectancy_r: float | None
    oos_trades: int
    holm_p: float | None
    regime_allowed: bool
    adv: float | None
    max_corr: float = 0.0
    components: dict = field(default_factory=dict)
    score: float = 0.0
    status: str = "REJECT"
    reasons: list = field(default_factory=list)
    explanation: dict = field(default_factory=dict)

    def to_row(self):
        d = asdict(self)
        return d


def _clip(x, a=0.0, b=1.0):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return a
    return max(a, min(b, x))


def score(o: Opportunity, stale_limit_s: float = 180, max_spread_bps: float = 25) -> Opportunity:
    stop_bps = (o.price - o.stop) / o.price * 1e4 if o.price and o.stop and o.stop < o.price else None
    c = {
        "edge": _clip((o.oos_expectancy_r or 0) / 0.3),
        "reliability": _clip(1 - (o.holm_p if o.holm_p is not None else 1.0)) * _clip(o.oos_trades / 300),
        "signal": _clip(math.tanh(max(0.0, o.strength) / 3.0)) if o.signal else 0.0,
        "regime": 1.0 if o.regime_allowed else 0.0,
        "liquidity": _clip(math.log10(max(o.adv or 1, 1)) / 10 - 0.6, 0, 1) * 2.5 if o.adv else 0.0,
        "cost": _clip(1 - (o.est_cost_bps / stop_bps) * 2) if stop_bps else 0.0,
        "freshness": _clip(1 - o.data_age_s / stale_limit_s),
        "diversification": _clip(1 - max(0.0, o.max_corr)),
    }
    c["liquidity"] = _clip(c["liquidity"])
    o.components = {k: round(v, 3) for k, v in c.items()}
    o.score = round(sum(WEIGHTS[k] * c[k] for k in WEIGHTS), 1)
    return o


def classify(o: Opportunity, guardian_reasons: list[str] | None = None, stale_limit_s: float = 180,
             max_spread_bps: float = 25) -> Opportunity:
    """Hard requirements first; only EXECUTE may submit orders."""
    r = []
    if o.strategy_status not in ("validated", "experimental"):
        r.append(f"strategy {o.strategy} is {o.strategy_status} (not validated)")
    elif not o.strategy_approved:
        r.append(f"strategy {o.strategy} validated but awaiting owner approval")
    if not o.regime_allowed:
        r.append(f"regime {o.regime} not in the strategy's validated regimes")
    if o.data_age_s > stale_limit_s:
        r.append(f"data stale ({o.data_age_s:.0f}s)")
    if o.spread_bps > max_spread_bps:
        r.append(f"spread {o.spread_bps:.1f}bps > {max_spread_bps}")
    if (o.oos_expectancy_r or 0) <= 0:
        r.append("no positive validated net expectancy")
    if o.stop is None or not o.stop < o.price:
        r.append("no valid stop")
    if guardian_reasons:
        r.extend(f"guardian: {x}" for x in guardian_reasons)
    if o.signal and not r and o.score >= EXECUTE_MIN_SCORE:
        o.status = "EXECUTE"
        o.reasons = [f"all confirmations passed; score {o.score}"]
    elif o.signal and not any(x.startswith(("data stale", "spread", "no valid stop", "guardian")) for x in r):
        o.status = "WATCH"
        o.reasons = r or [f"score {o.score} below {EXECUTE_MIN_SCORE}"]
    elif not o.signal:
        o.status = "WATCH" if o.score >= 30 else "REJECT"
        o.reasons = ["entry conditions not met on the latest confirmed bar"] + r[:2]
    else:
        o.status = "REJECT"
        o.reasons = r
    return o
