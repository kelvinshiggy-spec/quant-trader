#!/bin/bash
# Every 5 minutes on the server: write a plain-text health report to the private bucket (so the owner, or
# Claude with the deploy key, can check the bot without SSH) and apply a new release if one was published.
# The report holds container states, health endpoints, resources and recent log lines; never .env contents.
set -uo pipefail
APP=/opt/quant-trader
BUCKET="gs://$(cat /etc/qt-bucket 2>/dev/null)"
cd "$APP/repo" || exit 0
DOMAIN=$(grep -E '^DASHBOARD_DOMAIN=' "$APP/.env" | cut -d= -f2-)
OUT=$(mktemp)
# dashboard self-test: signs in like a browser and loads every tab's data; a failure means a blank/broken tab.
# On failure the api container is restarted once and the test repeated; failures are logged as events.
# right after a deploy the api is still starting: give it up to 2 minutes before testing (no false alarm)
for _ in $(seq 1 24); do curl -s -m 5 -o /dev/null http://127.0.0.1:8080/api/health && break; sleep 5; done
SELFTEST=$(docker compose exec -T api python -m qt.api.selftest --record 2>&1)
if ! echo "$SELFTEST" | grep -q "dashboard_selftest: OK"; then
  docker compose restart api >/dev/null 2>&1 || true
  for _ in $(seq 1 12); do sleep 10; curl -s -m 5 -o /dev/null http://127.0.0.1:8080/api/health && break; done
  SELFTEST="$SELFTEST
after restarting api:
$(docker compose exec -T api python -m qt.api.selftest --record 2>&1)"
fi
{
  echo "time_utc: $(date -u +%FT%TZ)"
  echo "release: $(cat "$APP/release.txt" 2>/dev/null)"
  echo "uptime: $(uptime -p)"
  echo "dashboard: https://$DOMAIN"
  echo "$SELFTEST" | grep "dashboard_selftest:" | tail -1
  echo "--- containers"
  docker compose ps -a --format '{{.Name}} {{.State}} {{.Health}} {{.Status}}'
  echo "--- api /api/health (local)"
  curl -s -m 10 http://127.0.0.1:8080/api/health; echo
  echo "--- dashboard over public HTTPS"
  curl -s -m 15 -o /dev/null -w 'http=%{http_code} tls_verify=%{ssl_verify_result}\n' "https://$DOMAIN/api/health"
  echo "--- dashboard self-test (details)"
  echo "$SELFTEST"
  echo "--- heartbeats and recent events (database)"
  docker compose exec -T api python - <<'PY' 2>&1 | tail -40
import sqlite3
c = sqlite3.connect("file:/app/data/qt.db?mode=ro", uri=True, timeout=5)
for r in c.execute("SELECT component, ts, status FROM heartbeats"):
    print("hb", *r)
for r in c.execute("SELECT ts, level, kind, substr(message, 1, 200) FROM events ORDER BY rowid DESC LIMIT 20"):
    print("ev", *r)
for r in c.execute("SELECT ts, kind, substr(message, 1, 300) FROM events WHERE kind IN ('dashboard_client', "
                   "'dashboard_selftest') AND ts >= strftime('%Y-%m-%dT%H:%M:%S', 'now', '-2 hours') ORDER BY rowid DESC LIMIT 5"):
    print("dashboard_problem", *r)
for k in ("daily_cycle_done", "universe_date", "risk_limits"):
    r = c.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    print("kv", k, r[0] if r else None)
for r in c.execute("SELECT strategy, status, COUNT(*) FROM opportunities WHERE ts >= date('now') "
                   "GROUP BY strategy, status ORDER BY 3 DESC LIMIT 15"):
    print("opp", *r)
for r in c.execute("SELECT ts, symbol, strategy, status, round(score,1), substr(reason, 1, 160) FROM opportunities "
                   "WHERE ts >= date('now') AND status != 'REJECT' ORDER BY score DESC LIMIT 8"):
    print("top", *r)
for r in c.execute("SELECT symbol, strategy, status, notional, entry_price, stop_price FROM trades "
                   "WHERE status IN ('open', 'pending') OR session >= date('now')"):
    print("trade", *r)
PY
  echo "--- container resources"
  docker stats --no-stream --format '{{.Name}} cpu={{.CPUPerc}} mem={{.MemUsage}}'
  echo "--- resources"
  free -m | head -3; df -h / | tail -1
  echo "--- clock"
  timedatectl show -p NTPSynchronized --value | sed 's/^/ntp_synchronized=/'
  echo "--- engine log (last 60 lines)"
  docker compose logs --no-color --tail 60 engine 2>&1 | sed -E 's/(key|secret|token|password)[^ ]*=[^ ]+/\1=<redacted>/Ig'
  echo "--- api log (last 20 lines)"
  docker compose logs --no-color --tail 20 api 2>&1 | sed -E 's/(key|secret|token|password)[^ ]*=[^ ]+/\1=<redacted>/Ig'
  echo "--- research log (last 30 lines)"
  docker compose logs --no-color --tail 30 research 2>&1 | sed -E 's/(key|secret|token|password)[^ ]*=[^ ]+/\1=<redacted>/Ig'
} > "$OUT" 2>&1
gcloud storage cp "$OUT" "$BUCKET/status/health.txt" >/dev/null || echo "qt-health: upload to $BUCKET failed" >&2
cat "$OUT"   # also to stdout: journal, and Cloud Logging when run from the boot script
rm -f "$OUT"

# self-update: a new release pointer -> re-run the boot script (idempotent; the engine reconciles on restart)
NEW=$(gcloud storage cat "$BUCKET/release.txt" 2>/dev/null || true)
# a settings-only change (new .env in Secret Manager, same code) must be applied too
PROJECT=$(curl -s -m 5 -H "Metadata-Flavor: Google" http://metadata.google.internal/computeMetadata/v1/project/project-id)
NEWENV=$(gcloud secrets versions access latest --secret=qt-env --project="$PROJECT" 2>/dev/null \
  | sed "s/^DASHBOARD_DOMAIN=auto\$/DASHBOARD_DOMAIN=$DOMAIN/" || true)
ENV_CHANGED=0
if [ -n "$NEWENV" ] && [ "$NEWENV" != "$(cat "$APP/.env")" ]; then ENV_CHANGED=1; fi
if { [ -n "$NEW" ] && [ "$NEW" != "$(cat "$APP/release.txt" 2>/dev/null)" ]; } || [ "$ENV_CHANGED" = 1 ]; then
  google_metadata_script_runner startup >/var/log/qt-update.log 2>&1 || true
fi
