# Status: completed vs incomplete (honest list)

"Verified" means exercised by automated tests and/or a real call to the Alpaca paper API, not just written.

## Completed and verified

| Area | Verification |
|---|---|
| Paper/live isolation, fail-closed config, key-type checks | unit tests |
| $100 virtual ledger, T+1 settled-cash model, reservations, partial fills, no shorting/adding | unit tests |
| Market calendar: holidays, early closes, DST, PT schedule | unit tests (Thanksgiving week 2026) |
| Historical data (SIP 5-min since 2021, daily since 2016, crypto hourly since 2021) with incremental cache | real downloads: 40 symbols, ~8M bars |
| 16 strategies (A-N incl. crypto, swing, sector rotation, research-backed intraday momentum) | causality tests (no look-ahead) |
| Backtester: next-bar fills, gap-through stops, stop-before-target, trade-through targets, EOD flatten, costs, rejects, T+1, guardian in the loop | unit tests on hand-built bars |
| Walk-forward + untouched holdout + Holm correction + 2x cost stress + Monte Carlo + $100 account simulation + experiment registry | full run on real data (see RESEARCH_RESULTS.md) |
| Filter ablation (relative volume) and a logistic-regression meta-label experiment | in the research run |
| Capital Guardian: sizing, all limits, cooldown, correlation, giveback, principal protection, stale data, spread | unit tests |
| Order manager: write-ahead, idempotent ids, uncertain-submit resolution, partial fills, rejects, timeouts | unit tests with simulated broker faults |
| Position manager: broker stop, trailing via replace, software stop backup, target/thesis/time exits, EOD flatten | unit + end-to-end tests |
| Restart recovery: fills while offline, stale positions flattened, reconciliation before trading | tests |
| Unexpected-position, clock-skew, broker-outage detection | tests |
| Fractional order capabilities | probed on the paper account |
| Live engine against the real paper API: startup, reconciliation, discovery universe (600 assets), snapshots, stage-1 ranking | run 2026-11-27 (market closed) |
| Dashboard API with auth, CSRF header, confirmations, ceiling enforcement; 10-tab mobile dashboard | API tests |
| Reports (daily/weekly/monthly + CSV), notifications (Telegram/email, levels) | code + report test via engine |
| Docker stack: restart policies, health checks, autoheal, log rotation, backups, HTTPS proxy | compose file; server deployment pending |

## Incomplete / limitations (not hidden)

* **Live order execution has not yet run during market hours.** The first real paper session will be the
  first time fills, stop replacement and EOD flatten happen at Alpaca. The code path is tested end to end
  against a simulated broker only.
* **Crypto execution is not wired** to the live engine (24/7 loop, crypto stop-limit behaviour). Crypto
  strategies are researched only. Requires `ALLOW_CRYPTO_EXECUTION` plus a build step.
* **Overnight (swing) execution is off** (`ALLOW_OVERNIGHT_HOLDS=false`), as the spec requires. The daily
  execution path exists in the engine but is only exercised by unit code paths, not live.
* **Options**: not implemented (research-only was optional; no options data used).
* **Opportunity score is uncalibrated.** It is a weighted ranking index; calibration needs forward trades.
* **Strategy portfolios** (multiple strategies combined in one simulation) are not yet simulated together;
  each strategy is simulated alone on the $100 account. Live, the guardian arbitrates between strategies.
* **Order book imbalance / market breadth**: not used (free plan has no depth data; breadth is approximated
  by sector-ETF participation).
* **News**: not integrated (kept disabled; no paid services).
* **Data**: research uses SIP bars; live uses IEX (one exchange). Prices agree closely; volume features are
  normalised within each feed. IEX spreads are wider than the true NBBO, which makes the spread filter
  conservative.
* **Survivorship bias**: the research universe was chosen in 2026 and contains survivors.
* **Spreads are estimated** from liquidity tiers (no free historical quotes) and stress-tested at 2x.
* **Trading halts** in backtests are not modelled explicitly (bars simply stop); live halts are detected
  as stale quotes and block entries.
* **Websocket streams** are not used; quotes are polled every 15 s and fills are reconciled every cycle.
  This is simpler and robust but adds up to ~15 s latency. The system is not, and does not claim to be,
  high-frequency.
