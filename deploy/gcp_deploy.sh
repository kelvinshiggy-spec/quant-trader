#!/usr/bin/env bash
# Deploy or update the bot on a Google Cloud e2-micro (Always Free machine type in us-west1/us-central1/us-east1).
#
# Runs from any machine with gcloud. No GitHub token is needed: the committed code is shipped as a tarball to a
# private Cloud Storage bucket in YOUR project, and the server pulls it. Secrets (.env) go to Secret Manager and
# are never written to instance metadata or logs.
#
# Required env:
#   GCP_PROJECT     project id
#   ENV_FILE        path to the production .env (built by deploy/make_env.py; never committed)
# Optional:
#   GCP_SA_KEY_JSON contents of a service-account JSON key, or GCP_SA_KEY_B64 the same key base64-encoded on
#                   one line (otherwise the current gcloud login is used)
#   GCP_REGION/GCP_ZONE (us-west1 / us-west1-b), VM_NAME (quant-trader), SHIP_DATA=1 (seed DB + market-data cache)
#
# Cost when used as written: e2-micro + 30 GB standard disk are inside the Always Free tier; the static external
# IPv4 address is billed by Google at about $0.005/hour (~$3.60/month). Bucket use stays inside the free 5 GB.
set -euo pipefail
PROJECT="${GCP_PROJECT:?set GCP_PROJECT}"
REGION="${GCP_REGION:-us-west1}"
ZONE="${GCP_ZONE:-${REGION}-b}"
NAME="${VM_NAME:-quant-trader}"
BUCKET="${GCS_BUCKET:-${PROJECT}-qt-deploy}"
ENV_FILE="${ENV_FILE:?set ENV_FILE}"
SHIP_DATA="${SHIP_DATA:-1}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
umask 077

if [ -n "${GCP_SA_KEY_JSON:-}" ] || [ -n "${GCP_SA_KEY_B64:-}" ]; then
  export CLOUDSDK_CONFIG="$WORK/gcloud"
  unset CLOUDSDK_AUTH_ACCESS_TOKEN || true
  if [ -n "${GCP_SA_KEY_JSON:-}" ]; then
    printf '%s' "$GCP_SA_KEY_JSON" > "$WORK/key.json"
  else
    printf '%s' "$GCP_SA_KEY_B64" | tr -d ' \n\r' | base64 -d > "$WORK/key.json"
  fi
  gcloud auth activate-service-account --key-file "$WORK/key.json" --quiet >/dev/null 2>&1
  rm -f "$WORK/key.json"
fi
G="gcloud --quiet --project=$PROJECT"
step() { echo "==> $*"; }

step "enabling APIs"
$G services enable compute.googleapis.com secretmanager.googleapis.com storage.googleapis.com iam.googleapis.com

SA="qt-vm-runner@${PROJECT}.iam.gserviceaccount.com"
step "VM service account"
$G iam service-accounts describe "$SA" >/dev/null 2>&1 || \
  $G iam service-accounts create qt-vm-runner --display-name "quant-trader VM (reads its secret and bucket only)"

step "bucket gs://$BUCKET"
$G storage buckets describe "gs://$BUCKET" >/dev/null 2>&1 || \
  $G storage buckets create "gs://$BUCKET" --location="$REGION" --default-storage-class=STANDARD \
     --uniform-bucket-level-access --public-access-prevention
retry() { for i in 1 2 3 4 5 6; do "$@" && return 0; sleep $((i * 5)); done; return 1; }  # new SAs take a moment to propagate
retry $G storage buckets add-iam-policy-binding "gs://$BUCKET" --member="serviceAccount:$SA" \
  --role=roles/storage.objectAdmin >/dev/null

step "secret qt-env"
$G secrets describe qt-env >/dev/null 2>&1 || $G secrets create qt-env --replication-policy=automatic >/dev/null
$G secrets versions add qt-env --data-file="$ENV_FILE" >/dev/null
# keep only the newest version (free tier: 6 active versions)
for v in $($G secrets versions list qt-env --filter="state=ENABLED" --sort-by=~createTime --format="value(name.basename())" | tail -n +2); do
  $G secrets versions destroy "$v" --secret=qt-env >/dev/null
done
retry $G secrets add-iam-policy-binding qt-env --member="serviceAccount:$SA" \
  --role=roles/secretmanager.secretAccessor >/dev/null

step "release bundle"
REV="$(git -C "$ROOT" rev-parse --short HEAD)"
if ! git -C "$ROOT" diff --quiet HEAD -- qt scripts deploy Dockerfile docker-compose.yml requirements.txt; then
  echo "refusing to deploy: uncommitted changes in deployed paths (commit first so the release is auditable)"; exit 1
fi
REL="release-${REV}.tar.gz"
git -C "$ROOT" archive --format=tar.gz -o "$WORK/$REL" HEAD
$G storage cp "$WORK/$REL" "gs://$BUCKET/releases/$REL" >/dev/null
if [ "$SHIP_DATA" = "1" ]; then
  # seed: research DB (no trading state, no secrets) + historical market-data cache, used only on first boot
  if [ -f "$ROOT/data/qt.db" ]; then
    python3 - "$ROOT/data/qt.db" "$WORK/seed.db" <<'EOF'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1]); dst = sqlite3.connect(sys.argv[2]); src.backup(dst)
for t in ("orders", "trades", "opportunities", "equity", "events", "commands", "heartbeats", "shadow_trades"):
    dst.execute(f"DELETE FROM {t}")
dst.execute("DELETE FROM kv WHERE k NOT IN ('last_research_run')")
dst.commit(); dst.execute("VACUUM"); dst.close()
EOF
    $G storage cp "$WORK/seed.db" "gs://$BUCKET/seed/qt.db" >/dev/null
  fi
  if [ -d "$ROOT/data/cache" ] && ! $G storage objects describe "gs://$BUCKET/seed/cache.tar" >/dev/null 2>&1; then
    tar -C "$ROOT/data" -cf "$WORK/cache.tar" cache
    $G storage cp "$WORK/cache.tar" "gs://$BUCKET/seed/cache.tar" >/dev/null
    rm -f "$WORK/cache.tar"
  fi
fi
printf '%s' "$REL" > "$WORK/release.txt"
$G storage cp "$WORK/release.txt" "gs://$BUCKET/release.txt" >/dev/null

step "static IP"
$G compute addresses describe qt-ip --region "$REGION" >/dev/null 2>&1 || \
  $G compute addresses create qt-ip --region "$REGION" --network-tier=PREMIUM
IP="$($G compute addresses describe qt-ip --region "$REGION" --format='value(address)')"

step "firewall (80/443 only; SSH stays as the project default)"
$G compute firewall-rules describe qt-web >/dev/null 2>&1 || \
  $G compute firewall-rules create qt-web --allow tcp:80,tcp:443 --target-tags qt-web --direction INGRESS

sed -e "s|__BUCKET__|$BUCKET|g" -e "s|__PROJECT__|$PROJECT|g" "$ROOT/deploy/vm_boot.sh" > "$WORK/boot.sh"
if ! $G compute instances describe "$NAME" --zone "$ZONE" >/dev/null 2>&1; then
  step "creating VM $NAME"
  $G compute instances create "$NAME" --zone "$ZONE" --machine-type e2-micro \
    --image-family debian-12 --image-project debian-cloud --boot-disk-size 30GB --boot-disk-type pd-standard \
    --address "$IP" --network-tier=PREMIUM \
    --service-account "$SA" --scopes cloud-platform --tags qt-web \
    --shielded-secure-boot --shielded-vtpm --shielded-integrity-monitoring \
    --maintenance-policy=MIGRATE --restart-on-failure \
    --metadata-from-file startup-script="$WORK/boot.sh" --metadata enable-oslogin=TRUE
else
  step "updating VM $NAME (the server picks up the new release within 5 minutes, no reboot)"
  $G compute instances add-metadata "$NAME" --zone "$ZONE" --metadata-from-file startup-script="$WORK/boot.sh"
fi
echo "release: $REL"
echo "dashboard: https://${IP//./-}.sslip.io"
echo "health:    gcloud storage cat gs://$BUCKET/status/health.txt"
