"""Strategy interface shared by the backtester and the live engine.

A strategy maps a CONFIRMED-bar feature frame to a signal frame. Orders are
always executed on a later bar (next bar open in backtests; next tick in live),
never at the signal bar's own close. The same `signals()` code path runs in
research and production, so live behaviour matches what was tested.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field

import numpy as np
import pandas as pd


@dataclass
class ExitPlan:
    trail_atr: float = 0.0        # 0 disables the trailing stop
    trail_activate_r: float = 1.0 # trailing starts once price is this many R in favour
    breakeven_r: float = 0.0      # move stop to entry after this many R (0 = off)
    target_r: float = 0.0         # fixed target in R (0 = none, unless a `target` column is set)
    max_bars: int = 0             # time exit (0 = none)
    flatten_eod: bool = True      # intraday: exit before the close

    def describe(self) -> str:
        parts = []
        if self.trail_atr:
            parts.append(f"trail {self.trail_atr}xATR after {self.trail_activate_r}R")
        if self.breakeven_r:
            parts.append(f"breakeven at {self.breakeven_r}R")
        if self.target_r:
            parts.append(f"target {self.target_r}R")
        if self.max_bars:
            parts.append(f"time exit {self.max_bars} bars")
        if self.flatten_eod:
            parts.append("flat before close")
        return ", ".join(parts) or "stop / thesis exit only"


SIGNAL_COLS = ["entry", "stop", "target", "exit_sig", "strength"]


@dataclass
class Strategy:
    params: dict = field(default_factory=dict)

    name: str = "base"
    letter: str = "?"
    version: str = "1.0.0"
    timeframe: str = "5Min"
    asset_class: str = "us_equity"
    overnight: bool = False
    description: str = ""
    grid: dict = field(default_factory=dict)  # param name -> list of values (kept SMALL)
    defaults: dict = field(default_factory=dict)

    def __post_init__(self):
        merged = dict(self.defaults)
        merged.update(self.params or {})
        self.params = merged

    @property
    def key(self) -> str:
        p = ",".join(f"{k}={v}" for k, v in sorted(self.params.items()))
        return f"{self.name}@{self.version}[{p}]"

    def p(self, k):
        return self.params[k]

    def exit_plan(self) -> ExitPlan:
        return ExitPlan()

    def signals(self, f: pd.DataFrame) -> pd.DataFrame:
        raise NotImplementedError

    def explain(self, row: pd.Series) -> dict:
        """Which conditions passed on this bar (for auditable trade explanations)."""
        return {}

    @classmethod
    def param_sets(cls) -> list[dict]:
        inst = cls()
        if not inst.grid:
            return [dict(inst.defaults)]
        keys = list(inst.grid)
        out = []
        for combo in itertools.product(*[inst.grid[k] for k in keys]):
            d = dict(inst.defaults)
            d.update(dict(zip(keys, combo)))
            out.append(d)
        return out


def frame(f: pd.DataFrame, entry, stop, target=None, exit_sig=None, strength=None) -> pd.DataFrame:
    out = pd.DataFrame(index=f.index)
    out["entry"] = pd.Series(entry, index=f.index).fillna(False).astype(bool)
    out["stop"] = pd.Series(stop, index=f.index).astype(float)
    out["target"] = np.nan if target is None else pd.Series(target, index=f.index).astype(float)
    out["exit_sig"] = False if exit_sig is None else pd.Series(exit_sig, index=f.index).fillna(False).astype(bool)
    out["strength"] = 0.0 if strength is None else pd.Series(strength, index=f.index).astype(float).fillna(0.0)
    # a valid entry needs a stop strictly below the close
    bad = ~(out["stop"] < f["close"]) | out["stop"].isna()
    out.loc[bad, "entry"] = False
    return out
