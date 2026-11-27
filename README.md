# quant-trader

An autonomous, evidence-driven quantitative **paper-trading** system on Alpaca, with an internal
**$100 virtual account**, a non-bypassable risk authority (Capital Guardian), walk-forward research,
a champion/challenger Strategy Tournament and a mobile-friendly dashboard.

> Paper trading only. No real money is used, and live trading cannot be switched on from the dashboard.
> Nothing here guarantees profits or capital protection. Stops, gaps, slippage and outages can produce
> losses larger than planned.

## How it works

```
               +-------------------- research container (nightly, after the close) -------------------+
               | Alpaca SIP history -> features -> 16 strategies x small param grids -> backtester     |
               | -> walk-forward (12m train / 3m test) -> untouched holdout -> Holm correction          |
               | -> cost stress 2x -> Monte Carlo -> $100 account simulation -> experiment registry     |
               | -> strategy_status (validated / shadow / rejected / suspended)                          |
               +---------------------------------------------+-----------------------------------------+
                                                             | SQLite (WAL)
+-------------------------------- engine container (always on) -----------------------------------------+
| Stage 1 scan (snapshots, ~600 liquid assets) -> watchlist (30) -> Stage 2 on each confirmed 5-min bar  |
| -> strategy signals -> regime check -> score 0-100 -> EXECUTE / WATCH / REJECT                         |
| -> Capital Guardian (sizing + all limits) -> order manager (idempotent) -> broker stop -> trailing,    |
|    targets, thesis/time exits, EOD flatten -> $100 ledger (T+1 settlement) -> reports / notifications  |
+------------------------------------------------+-------------------------------------------------------+
                                                 |
                      api container (FastAPI, auth) + Caddy HTTPS  ->  dashboard
```

Only strategies that are **validated by research** can place paper orders. Approval is either one click in
the Tournament tab or, if the owner opted in with `AUTO_APPROVE_VALIDATED=true` (the deployed setting),
automatic; an approved strategy that fails a later run or its forward test is suspended automatically. Everything else runs in **shadow mode**: its live signals are tracked as
forward tests without orders. If nothing qualifies, the bot stays in cash.

## Repository layout

| Module | Purpose |
|---|---|
| `qt/config.py` | Settings, fail-closed paper/live isolation, risk limits + approved ceilings |
| `qt/ledger.py` | Independent $100 ledger: settled/unsettled cash (T+1), reservations, positions, P&L |
| `qt/guardian.py` | Capital Guardian: sizing, daily/weekly/drawdown limits, cooldowns, correlation, giveback, principal protection |
| `qt/costs.py` | Spread/slippage/fee model shared by backtests and live pre-trade checks |
| `qt/indicators.py`, `qt/strategies/` | Causal indicators, feature engineering, 16 strategies (A-N) |
| `qt/regime.py` | Market regime engine (trend, volatility, selloff, breadth) |
| `qt/backtest/` | Event-driven backtester + metrics/statistics |
| `qt/research/` | Datasets, walk-forward, tournament/registry, nightly scheduler, backups |
| `qt/scanner.py`, `qt/scoring.py` | Two-stage scanner, opportunity scoring and classification |
| `qt/broker/` | Alpaca adapter (alpaca-py) + simulated broker for tests |
| `qt/execution.py` | Order manager (write-ahead, idempotent) + position manager (stops, trails, exits) |
| `qt/engine.py` | Live engine loop, reconciliation, commands, schedule |
| `qt/reporting.py`, `qt/notify.py` | Daily/weekly/monthly reports + CSV, Telegram/email alerts |
| `qt/api/`, `qt/dashboard/` | Authenticated API and the dashboard |
| `deploy/` | Server install script, Google Cloud provisioning |
| `docs/` | Alpaca findings, research results, limitations |

## Local development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env            # fill in PAPER keys locally; never commit .env
pytest -q                       # full test suite
python -m scripts.fetch_data    # download research history (free plan)
python -m qt.research.runner --workers 2
python -m qt.engine             # live paper engine
python -m qt.api.app            # dashboard on http://127.0.0.1:8080
```

## Deployment (always-on server)

**Google Cloud (used for this bot).** From any machine with `gcloud`:

```bash
ALPACA_PAPER_API_KEY=... ALPACA_PAPER_API_SECRET=... DASHBOARD_PASSWORD=... \
  python -m deploy.make_env /secure/path/prod.env --overnight --auto-approve
GCP_PROJECT=<project-id> ENV_FILE=/secure/path/prod.env bash deploy/gcp_deploy.sh
```

`gcp_deploy.sh` creates (idempotently) an e2-micro VM in us-west1 with a 30 GB standard disk (Always Free
machine and disk), a static IP (billed by Google, about $3.60/month), a firewall rule for 80/443, a VM
service account that can read only its own secret and bucket, a private Cloud Storage bucket, and the
`qt-env` secret in Secret Manager. No GitHub token is needed: the committed code is shipped to the bucket as
`releases/release-<commit>.tar.gz`, and the first boot also gets a seed research DB and the market-data
cache. On every boot `deploy/vm_boot.sh` pulls the secret and release and runs `deploy/install.sh`.
A systemd timer (`deploy/vm_health.sh`) writes `status/health.txt` to the bucket every 5 minutes and applies
a newly published release without a reboot. Re-running `gcp_deploy.sh` publishes an update.

Check the server without SSH: `gcloud storage cat gs://<project>-qt-deploy/status/health.txt`.

**Any other Linux VM** (Debian 12 / Ubuntu 22.04+, 1 GB RAM + swap is enough):
create `/opt/quant-trader/.env`, then
`REPO_URL=https://x-access-token:<read-only token>@github.com/kelvinshiggy-spec/quant-trader.git REPO_REF=main bash deploy/install.sh`.

What you get either way: Docker installs and starts on boot; every container has `restart: unless-stopped`;
health checks on engine/API/research; `autoheal` restarts unhealthy containers; logs rotate; the
database is backed up daily (14 kept, `data/backups`); Caddy serves the dashboard over HTTPS with an
automatic certificate (`<ip-with-dashes>.sslip.io` works without buying a domain).

Operations:
```bash
cd /opt/quant-trader/repo
docker compose ps                      # status + health
docker compose logs -f engine          # live log
docker compose restart engine          # restart (reconciles before trading)
git pull && docker compose up -d --build   # update to a new approved version
```

## Safety model

* **Fail closed**: unknown `TRADING_MODE`, missing keys, a live-looking key in the paper slot, identical
  paper/live keys, or clock drift > 10 s vs the broker all stop trading.
* **Live trading** needs `TRADING_MODE=live`, `LIVE_TRADING_ENABLED=true`, the exact confirmation phrase,
  separate live keys, AND a code change (the Alpaca adapter refuses `paper=False` in this build).
* **$100 enforcement**: every buy reserves ledger cash first. In the default `cash` account model only
  settled funds can be used and stock sale proceeds settle T+1, exactly like a real $100 cash account.
* **Guardian limits** (initial experimental settings, not proven optimal): 0.5% risk/trade, 2% daily,
  5% weekly, 10% drawdown (latched; you reset it), 2 positions, 50% per position, 80% total, no leverage,
  margin, shorting, options, averaging down or martingale; 3-loss cooldown; stale data, spread,
  abnormal volatility, correlation, halt and connectivity protection.
* **Dashboard** can only tighten limits. Raising a ceiling requires editing `QT_RISK_CEILING_JSON` on the
  server (your explicit approval).
* **Restarts**: the engine reconciles orders and positions before trading, applies fills that happened
  while it was down, and flattens intraday positions left from a previous session.

## Research results

See [`docs/RESEARCH_RESULTS.md`](docs/RESEARCH_RESULTS.md) for the actual verified results, every
failed experiment, and limitations.

## Known limitations

See [`docs/STATUS.md`](docs/STATUS.md) for completed vs incomplete functionality.
