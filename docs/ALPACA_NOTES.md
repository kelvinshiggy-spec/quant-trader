# Alpaca integration notes (verified 2026-11-27)

Facts below were checked against the official docs (docs.alpaca.markets, pages modified 2026-11-24)
and, where marked **probed**, by submitting and cancelling real orders on this paper account.

| Topic | Finding | Source |
|---|---|---|
| SDK | `alpaca-py` 0.45.0 (official) | PyPI |
| Fractional order types | market, limit, stop, stop_limit only | docs + **probed** |
| Fractional TIF | DAY only ("fractional orders must be DAY orders") | **probed** |
| Fractional trailing stop | REJECTED | **probed** |
| Fractional bracket/OCO/OTO | REJECTED ("fractional orders must have order_class=simple") | **probed** |
| Whole-share bracket | accepted | **probed** |
| client_order_id | uniqueness enforced ("client_order_id must be unique") -> idempotent retries | **probed** |
| Notional orders | up to 2 decimals, qty up to 9 decimals; $1 minimum per docs | docs |
| Sub-penny | >= $1: 2 decimals; < $1: 4 decimals | docs |
| Extended hours | limit orders only, TIF day/gtc | docs |
| Crypto orders | market, limit, stop_limit; TIF gtc/ioc; no margin; no shorting; max $200k notional | docs |
| Crypto fees (tier 1, < $100k 30d volume) | maker 0.15%, taker 0.25% | docs |
| PDT | FINRA Rule 4210 intraday-margin standards replaced the $25k PDT rule (effective 2026-06-04, phase-in to 2027-10-20). Alpaca removed `pattern_day_trader` from responses 2026-07-06. Cash accounts: settled-funds rules still apply. | FINRA RN 26-10, SDK warning |
| Market data (free plan) | IEX real time; SIP history except the latest 15 minutes ("subscription does not permit querying recent SIP data") | **probed** |
| SIP daily history | available from 2016 | **probed** |
| Paper account | $100,000 default, 2x multiplier, shorting enabled | **probed** (the bot ignores all of this and enforces its own $100 cash ledger) |

## Consequences for the design

* **Protective stops**: fractional positions get a broker-side `stop` (DAY) sell order right after the
  entry fills. Trailing is done by the engine via `replace_order`. Profit targets are software-managed
  (a second sell order would lock the same shares). A software stop also watches every position in case
  the broker stop is missing, rejected, or the price gaps through it.
  **Software-managed exit risk**: if the engine is offline, trailing and targets pause; the last broker
  stop still protects the position until the close, when DAY orders expire. Intraday positions are
  flattened by the engine 10 minutes before the close; if the engine is down at that moment the
  position would remain overnight with no stop. On restart the engine flattens any stale position first.
* **Duplicate prevention**: deterministic client ids (`qt-<trade>-entry`, `-stop<n>`, `-exit<n>`).
* **Live trading**: `AlpacaBroker` refuses `paper=False` in this build.

## Overnight (swing) positions

Enabled on the server by the owner (`ALLOW_OVERNIGHT_HOLDS=true`, 2026-11-27).
* Fractional stops must be DAY orders, so the broker stop expires at each close. The engine re-places it
  in the first regular-hours cycle of the next session (within ~15 s of the open) with a new client id
  (`-stop<n>`), at the same or a higher (trailed) price.
* Outside regular hours the engine does bookkeeping only: no stop submissions and no software exits on
  thin extended-hours quotes.
* **Gap risk**: a stock that opens below its stop is sold at the open, which can be well below the stop.
  Overnight news can therefore produce losses larger than the planned 0.5% of equity. The backtester
  models this (gap-through stops fill at the open).
