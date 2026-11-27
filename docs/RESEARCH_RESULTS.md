# Research results (run 20261127T003331Z, verified)

These numbers come from `data/reports/research_20261127T003331Z.json`, produced by
`python -m qt.research.runner --workers 2` on real Alpaca history. Nothing below is projected or
hand-edited. The research database (`experiments`, `strategy_status`) holds the same results.

**Bottom line: no strategy passed validation. The champion slot is empty, so the bot will place no
paper orders and stays in cash.** All strategies keep running in shadow mode so their live signals are
tracked as forward tests. This is the correct outcome given the evidence, not a malfunction.

## Setup

| Item | Value |
|---|---|
| Universe | 40 symbols: core/sector ETFs, 20 large-cap stocks, BTC/ETH/SOL (`qt/universe.py`) |
| Data | Alpaca SIP 5-min bars (2021+), SIP daily (2016+), crypto hourly (2021+), split/dividend adjusted |
| Development period | 2021-01-01 .. 2024-12-31 (walk-forward: 12-month train, 3-month test, rolling) |
| Untouched holdout | 2025-01-01 .. 2026-11-25, evaluated once after parameters were frozen |
| Costs | spread by liquidity tier, 2 bps slippage (5 bps crypto), SEC + TAF fees, crypto taker 25 bps, next-bar fills, random rejects |
| Multiple testing | Holm correction across 32 variants (16 strategies, each with and without regime gating) |
| Runtime | 691 s on 2 workers |
| Benchmark | SPY buy-and-hold over the holdout: **+25.0%** |

Validation criteria were fixed before the run (`qt/research/runner.py: CRITERIA`): at least 60 OOS
trades, Holm p ≤ 0.05, OOS profit factor ≥ 1.3, profit factor excluding the top 5% of winners ≥ 1.0,
profitable at 2x costs, at least 20 holdout trades with profit factor ≥ 1.1. They were not changed
after seeing the results.

## Results per variant

R = profit or loss divided by the planned risk at entry. "OOS" = concatenated walk-forward test windows.

| Variant | OOS trades | OOS exp R | OOS PF | PF ex top 5% | Holm p | 2x cost exp R | Holdout trades | Holdout exp R | Holdout PF | Decision |
|---|---|---|---|---|---|---|---|---|---|---|
| A_momentum | 5067 | -0.118 | 0.89 | 0.45 | 1.000 | -0.257 | 2481 | -0.141 | 0.93 | rejected |
| A_momentum+regime | 1452 | -0.044 | 0.97 | 0.53 | 1.000 | -0.162 | 450 | -0.232 | 0.56 | rejected |
| B_orb | 5418 | -0.055 | 0.86 | 0.40 | 1.000 | -0.112 | 3286 | -0.052 | 0.74 | rejected |
| B_orb+regime | 1452 | -0.051 | 0.97 | 0.46 | 1.000 | -0.118 | 0 | - | - | rejected |
| C1_vwap_reclaim | 11957 | -0.097 | 0.83 | 0.47 | 1.000 | -0.196 | 5251 | -0.101 | 0.79 | rejected |
| C1_vwap_reclaim+regime | 1442 | -0.120 | 0.71 | 0.40 | 1.000 | -0.192 | 0 | - | - | rejected |
| C2_vwap_reversion | 1079 | -0.234 | 0.66 | 0.38 | 1.000 | -0.399 | 78 | -0.017 | 0.73 | rejected |
| C2_vwap_reversion+regime | 5 | -0.578 | 0.17 | 0.05 | 1.000 | -0.673 | 0 | - | - | rejected |
| D_mean_reversion | 16905 | -0.228 | 0.68 | 0.43 | 1.000 | -0.373 | 11905 | -0.231 | 0.70 | rejected |
| D_mean_reversion+regime | 380 | -0.139 | 0.83 | 0.52 | 1.000 | -0.312 | 0 | - | - | rejected |
| **E_trend_following** (daily, overnight) | 249 | **0.434** | 1.98 | 0.89 | 0.170 | 0.415 | 130 | **0.590** | 2.17 | shadow |
| E_trend_following+regime | 125 | 0.335 | 1.51 | 0.77 | 1.000 | 0.316 | 93 | 0.303 | 1.52 | shadow |
| F_relative_strength | 6234 | -0.063 | 0.91 | 0.51 | 1.000 | -0.144 | 3536 | -0.063 | 0.91 | rejected |
| F_relative_strength+regime | 2052 | -0.049 | 0.87 | 0.51 | 1.000 | -0.132 | 663 | -0.154 | 0.69 | rejected |
| G_volume_breakout | 1087 | -0.046 | 0.87 | 0.51 | 1.000 | -0.147 | 441 | 0.023 | 1.67 | rejected |
| G_volume_breakout+regime | 536 | 0.004 | 0.94 | 0.54 | 1.000 | -0.110 | 283 | -0.124 | 0.82 | rejected |
| H_pullback | 3989 | -0.101 | 0.86 | 0.50 | 1.000 | -0.204 | 2614 | -0.135 | 0.86 | rejected |
| H_pullback+regime | 225 | -0.188 | 0.57 | 0.31 | 1.000 | -0.299 | 0 | - | - | rejected |
| I_intraday_reversal | 5943 | -0.157 | 0.79 | 0.49 | 1.000 | -0.267 | 4762 | -0.162 | 0.78 | rejected |
| I_intraday_reversal+regime | 1763 | 0.013 | 1.14 | 0.79 | 1.000 | -0.082 | 359 | -0.146 | 0.95 | rejected |
| J_vol_expansion | 4861 | -0.101 | 0.88 | 0.47 | 1.000 | -0.239 | 2515 | -0.177 | 0.99 | rejected |
| J_vol_expansion+regime | 694 | -0.181 | 0.67 | 0.32 | 1.000 | -0.310 | 0 | - | - | rejected |
| **K_sector_rotation** (daily, overnight) | 139 | 0.133 | 1.45 | 0.93 | 1.000 | 0.118 | 64 | 0.063 | 1.25 | shadow |
| K_sector_rotation+regime | 30 | 0.264 | 2.40 | 1.53 | 1.000 | 0.245 | 29 | 0.148 | 1.40 | shadow |
| L_crypto_trend | 390 | -0.285 | 0.59 | 0.28 | 1.000 | -0.584 | 262 | -0.244 | 0.55 | rejected |
| L_crypto_trend+regime | 2 | -1.154 | 0.00 | 0.00 | 1.000 | -1.467 | 29 | -0.642 | 0.20 | rejected |
| L2_crypto_reversion (v1.1.0) | 291 | -0.374 | 0.52 | 0.35 | 1.000 | -0.630 | 187 | -0.310 | 0.52 | rejected |
| L2_crypto_reversion+regime | 3 | -0.011 | 0.60 | 0.13 | 1.000 | -0.261 | 0 | - | - | rejected |
| **M_swing_rsi2** (daily, overnight) | 755 | 0.073 | 1.40 | **0.97** | **0.032** | 0.060 | 319 | 0.081 | 1.41 | shadow |
| M_swing_rsi2+regime | 456 | 0.045 | 1.31 | 0.89 | 1.000 | 0.032 | 319 | 0.081 | 1.41 | shadow |
| N_intraday_momentum | 8188 | -0.206 | 0.50 | 0.28 | 1.000 | -0.334 | 6712 | -0.158 | 0.65 | rejected |
| N_intraday_momentum+regime | 0 | - | - | - | 1.000 | - | 0 | - | - | rejected |

"Holdout trades 0" for regime-gated variants means the regimes they were allowed to trade in did not
occur in the holdout.

## The closest candidates (all overnight, which is disabled by spec)

* **M_swing_rsi2** (buy oversold RSI(2) dips in an uptrend, hold up to 10 days). The only variant that is
  significant after Holm correction (p = 0.032) and profitable at 2x costs and in the holdout. It fails
  one pre-registered check: profit factor without the top 5% of winners is 0.97 (< 1.0), so the edge
  leans on a few large winners. Holdout $100 simulation: **+2.0%**, max drawdown 2.6%, 130 trades, win
  rate 64%. SPY returned +25% over the same period.
* **E_trend_following** (daily ADX trend with a 3.5x ATR trail). Largest edge per trade (holdout +0.59 R)
  but only 249 OOS trades, so it is not significant after correction (Holm p = 0.17) and also depends on
  a few big winners. In the realistic $100 simulation it made **-0.2%** over the holdout (39 trades):
  the guardian's position caps and settled-cash rule let it take only a fraction of the signals.
* **K_sector_rotation**. Positive but not significant (Holm p = 1.0). $100 holdout simulation: +0.3%.

All three hold positions overnight. `ALLOW_OVERNIGHT_HOLDS=false` by spec, so even a validated version
would not trade until you enable overnight holds.

## Why the intraday strategies fail: costs

Gross vs net expectancy at default parameters over the full sample (`data/reports/gross_vs_net_default_params.csv`):

| Strategy | Trades | Mean R gross (no costs) | Mean R net (realistic costs) |
|---|---|---|---|
| A_momentum | 29,557 | +0.043 | -0.160 |
| B_orb | 11,250 | +0.044 | -0.076 |
| C1_vwap_reclaim | 32,151 | +0.029 | -0.093 |
| C2_vwap_reversion | 282 | -0.052 | -0.287 |
| D_mean_reversion | 27,969 | -0.028 | -0.243 |
| F_relative_strength | 20,826 | +0.034 | -0.083 |
| G_volume_breakout | 3,772 | +0.047 | -0.104 |
| H_pullback | 13,522 | +0.012 | -0.138 |
| I_intraday_reversal | 3,665 | -0.033 | -0.162 |
| J_vol_expansion | 12,330 | +0.030 | -0.166 |
| N_intraday_momentum | 21,744 | -0.024 | -0.214 |

Several intraday signals have a small real gross edge (+0.03 to +0.05 R), but 5-minute stops are tight,
so spread plus slippage is a large fraction of the risk per trade, and costs turn every one of them
negative. More filtering, not more trading, is the only plausible route for the intraday strategies.

## Realistic $100 account simulations (holdout, best variant per strategy)

| Strategy | Trades | Return | Max drawdown |
|---|---|---|---|
| A_momentum+regime | 165 | -8.4% | 8.8% |
| B_orb+regime | 0 | 0.0% | 0.0% |
| C1_vwap_reclaim | 253 | -10.0% | 9.7% |
| C2_vwap_reversion | 70 | -1.4% | 3.0% |
| D_mean_reversion+regime | 0 | 0.0% | 0.0% |
| E_trend_following | 39 | -0.2% | 4.6% |
| F_relative_strength+regime | 168 | -9.6% | 9.6% |
| G_volume_breakout+regime | 230 | -6.4% | 7.0% |
| H_pullback | 287 | -9.0% | 9.8% |
| I_intraday_reversal+regime | 71 | -4.9% | 6.3% |
| J_vol_expansion | 346 | -9.1% | 9.9% |
| K_sector_rotation+regime | 26 | +0.3% | 2.4% |
| L_crypto_trend | 27 | -7.8% | 10.0% |
| L2_crypto_reversion+regime | 0 | 0.0% | 0.0% |
| **M_swing_rsi2** | 130 | **+2.0%** | 2.6% |
| N_intraday_momentum | 919 | -10.0% | 10.0% |

The losing simulations stop near -10% because the guardian's latched 10% drawdown halt works as designed.

## Other experiments (kept, including failures)

* **Relative-volume filter ablation** (development period). The filter improves every strategy it
  applies to but does not make any of them profitable. Example: A_momentum -0.184 R → -0.162 R;
  B_orb -0.114 → -0.075; G_volume_breakout -0.185 → -0.136.
* **Regime gating** helped A, G and I slightly in OOS but did not survive the holdout.
* **Logistic-regression meta-label** (predict which signals win). Failed: mean filtered expectancy
  -0.142 R vs -0.138 R unfiltered, and its Brier score beat the base-rate baseline in only 5 of 12
  windows. Not used in production.
* **L2_crypto_reversion v1.0.0** produced zero signals because of a contradictory filter. Fixed in
  v1.1.0 (the result above). Still rejected.

## Limitations of this research

See `docs/STATUS.md`. The main ones: the universe contains survivors chosen in 2026; spreads are
estimated, not measured; one research run on 40 symbols; holdout is 23 months of mostly rising markets.

# Round 2 (run 20261127T012404Z): daily and monthly strategies

Added after round 1 at the owner's request to look beyond intraday. Hypotheses come from published research
and were fixed before their first run: O time-series momentum (Moskowitz, Ooi & Pedersen 2012), P dual
momentum (Antonacci 2014), Q turn of the month (Lakonishok & Smidt 1988; McConnell & Xu 2008), R internal
bar strength reversion, S 52-week-high momentum (George & Hwang 2004). Universe: 16 asset-class ETFs
(O, P), index ETFs (Q), index + sector ETFs (R), all 54 daily symbols (S). Development period
2017-01-01..2024-12-31 (longer daily history), same untouched holdout 2025-01-01..2026-11-25, same
criteria. Holm correction now covers all **42** variants from both rounds, which is stricter for everyone.

**Bug found and fixed in this round.** The first round-2 run showed S_52w_high with an expectancy of
-50,303 R. Cause: a signal whose next open was already below its stop created a trade with near-zero
risk. The live guardian rejects such entries, so the backtester now skips them as well (test
`test_entry_skipped_when_open_gaps_below_stop`). All 42 variants were re-run with the fix. Round-1
numbers above changed only marginally (E's holdout trade count changed because its dev-period parameter
choice changed: adx 15 instead of 25).

| Variant | OOS trades | OOS exp R | OOS PF | PF ex top 5% | Holm p (42 tests) | 2x cost exp R | Holdout trades | Holdout exp R | Holdout PF | Decision |
|---|---|---|---|---|---|---|---|---|---|---|
| O_tsmom | 155 | 0.778 | 2.37 | 1.15 | 0.456 | 0.742 | 23 | 1.022 | 4.12 | shadow |
| O_tsmom+regime | 18 | 1.406 | 2.91 | 2.10 | 1.000 | 1.362 | 21 | 1.205 | 5.07 | rejected |
| P_dual_momentum | 51 | 0.173 | 1.24 | 0.53 | 1.000 | 0.159 | 7 | 2.645 | 5.53 | rejected |
| P_dual_momentum+regime | 0 | - | - | - | 1.000 | - | 4 | 1.117 | 2.90 | rejected |
| Q_turn_of_month | 324 | 0.045 | 1.33 | 1.00 | 1.000 | 0.031 | 74 | -0.041 | 1.02 | rejected |
| Q_turn_of_month+regime | 84 | 0.156 | 1.81 | 1.31 | 0.810 | 0.139 | 63 | -0.080 | 0.86 | shadow |
| R_ibs_reversion | 1858 | 0.032 | 1.30 | 1.01 | 0.410 | 0.013 | 416 | 0.063 | 1.24 | shadow |
| R_ibs_reversion+regime | 1179 | 0.023 | 1.18 | 0.91 | 1.000 | 0.004 | 333 | 0.061 | 1.20 | shadow |
| S_52w_high | 651 | 0.345 | 1.88 | 0.97 | 0.021 | 0.314 | 208 | 0.317 | 1.86 | shadow |
| S_52w_high+regime | 366 | 0.187 | 1.51 | 0.81 | 0.722 | 0.159 | 208 | 0.317 | 1.86 | shadow |

**Result: still no validated strategy; the bot stays in cash.** What came closest:

* **S_52w_high**: significant after correction (Holm p = 0.021), profitable at 2x costs and in the holdout
  (PF 1.86), but PF without the top 5% of winners is 0.97 < 1.0. $100 holdout simulation: +0.9%.
* **M_swing_rsi2**: unchanged; Holm p = 0.041 with 42 tests, same 0.97 robustness failure. $100 holdout: +2.0%.
* **O_tsmom**: the largest edge (OOS +0.78 R, holdout +1.02 R, PF 4.1 in the holdout) and robust without its
  top winners (1.15), but only 155 OOS trades in eight years, so it is not significant after correction
  (Holm p = 0.46). Monthly signals accumulate slowly; forward shadow trades will add evidence.
* R_ibs_reversion: real but thin (+0.03 R), PF 1.30 just short of 1.3, not significant.
* Q_turn_of_month: the regime-gated version failed the holdout (PF 0.86).

SPY buy-and-hold returned +25.0% over the holdout. None of these strategies beat it on the $100 account.
The nightly research job re-tests everything after each close; a strategy that passes trades paper
automatically (owner opt-in, 2026-11-27).
