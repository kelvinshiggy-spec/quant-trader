#!/usr/bin/env bash
# Provision the bot on a Google Cloud e2-micro (Always Free tier in us-west1 / us-central1 / us-east1).
# Runs from any machine with gcloud authenticated to YOUR project. Creates nothing paid beyond the free
# tier when used as written (1 e2-micro, 30 GB standard disk, no static IP reservation).
#
# Required env: GCP_PROJECT, GH_DEPLOY_TOKEN (read-only token for the private repo), and a local .env file.
# Secrets are stored in Secret Manager (free tier: 6 active secret versions), never in instance metadata.
set -euo pipefail
PROJECT="${GCP_PROJECT:?}"
ZONE="${GCP_ZONE:-us-west1-b}"
NAME="${VM_NAME:-quant-trader}"
REPO="${REPO:-kelvinshiggy-spec/quant-trader}"
REF="${REPO_REF:-main}"
ENV_FILE="${ENV_FILE:-.env}"
gcloud config set project "$PROJECT" >/dev/null
gcloud services enable compute.googleapis.com secretmanager.googleapis.com

SA="qt-vm@${PROJECT}.iam.gserviceaccount.com"
gcloud iam service-accounts describe "$SA" >/dev/null 2>&1 || \
  gcloud iam service-accounts create qt-vm --display-name "quant-trader VM"

put_secret() {  # name, file
  gcloud secrets describe "$1" >/dev/null 2>&1 || gcloud secrets create "$1" --replication-policy=automatic
  gcloud secrets versions add "$1" --data-file="$2" >/dev/null
  gcloud secrets add-iam-policy-binding "$1" --member="serviceAccount:$SA" --role=roles/secretmanager.secretAccessor >/dev/null
}
put_secret qt-env "$ENV_FILE"
printf '%s' "$GH_DEPLOY_TOKEN" > /tmp/qt_tok && put_secret qt-gh-token /tmp/qt_tok && rm -f /tmp/qt_tok

cat > /tmp/qt_startup.sh <<EOS
#!/bin/bash
set -e
mkdir -p /opt/quant-trader
gcloud secrets versions access latest --secret=qt-env > /opt/quant-trader/.env
chmod 600 /opt/quant-trader/.env
IP=\$(curl -s -H "Metadata-Flavor: Google" http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/access-configs/0/external-ip)
sed -i "s/^DASHBOARD_DOMAIN=auto\$/DASHBOARD_DOMAIN=\${IP//./-}.sslip.io/" /opt/quant-trader/.env
TOK=\$(gcloud secrets versions access latest --secret=qt-gh-token)
apt-get update -y && apt-get install -y git curl
curl -fsSL "https://raw.githubusercontent.com/${REPO}/${REF}/deploy/install.sh" -H "Authorization: token \$TOK" -o /tmp/install.sh
REPO_URL="https://x-access-token:\$TOK@github.com/${REPO}.git" REPO_REF="${REF}" bash /tmp/install.sh
EOS

if ! gcloud compute instances describe "$NAME" --zone "$ZONE" >/dev/null 2>&1; then
  gcloud compute instances create "$NAME" --zone "$ZONE" --machine-type e2-micro \
    --image-family debian-12 --image-project debian-cloud --boot-disk-size 30GB --boot-disk-type pd-standard \
    --service-account "$SA" --scopes cloud-platform --tags qt-web \
    --metadata-from-file startup-script=/tmp/qt_startup.sh
else
  gcloud compute instances add-metadata "$NAME" --zone "$ZONE" --metadata-from-file startup-script=/tmp/qt_startup.sh
  gcloud compute instances reset "$NAME" --zone "$ZONE"
fi
gcloud compute firewall-rules describe qt-web >/dev/null 2>&1 || \
  gcloud compute firewall-rules create qt-web --allow tcp:80,tcp:443 --target-tags qt-web
IP=$(gcloud compute instances describe "$NAME" --zone "$ZONE" --format='get(networkInterfaces[0].accessConfigs[0].natIP)')
echo "VM external IP: $IP  -> dashboard: https://${IP//./-}.sslip.io (set DASHBOARD_DOMAIN to this)"
