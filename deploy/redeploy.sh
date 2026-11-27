#!/usr/bin/env bash
# Rebuild the production .env from this machine's environment (keeping the server's session key and password
# hash when unchanged) and deploy. Owner choices are read from env: EXPERIMENTAL, MIN_DAILY_TRADES,
# SWING_WIDE_UNIVERSE, MAX_POSITIONS. Needs GCP_PROJECT and GCP_SA_KEY_B64 (or a gcloud login).
# The server's dashboard password is kept unless CHANGE_DASHBOARD_PASSWORD=1 (then DASHBOARD_PASSWORD is applied).
set -euo pipefail
cd "$(dirname "$0")/.."
umask 077
W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT
if [ -n "${GCP_SA_KEY_B64:-}" ]; then
  export CLOUDSDK_CONFIG="$W/gcloud"; unset CLOUDSDK_AUTH_ACCESS_TOKEN || true
  printf '%s' "$GCP_SA_KEY_B64" | tr -d ' \n\r' | base64 -d > "$W/key.json"
  gcloud auth activate-service-account --key-file "$W/key.json" --quiet >/dev/null 2>&1; rm -f "$W/key.json"
fi
gcloud --quiet --project="$GCP_PROJECT" secrets versions access latest --secret=qt-env > "$W/prev.env" 2>/dev/null || true
# stop here if the dashboard would load blank or broken (server self-test + phone-sized browser check)
python -m deploy.check_dashboard || { echo "dashboard check failed; not deploying"; exit 1; }
PREVIOUS_ENV_FILE="$W/prev.env" python -m deploy.make_env "$W/prod.env" --overnight --auto-approve \
  --experimental "${EXPERIMENTAL:-}" --min-daily-trades "${MIN_DAILY_TRADES:-0}"
ENV_FILE="$W/prod.env" SHIP_DATA="${SHIP_DATA:-0}" bash deploy/gcp_deploy.sh
