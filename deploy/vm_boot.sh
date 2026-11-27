#!/bin/bash
# Google Cloud startup script (runs as root on every boot). Placeholders are filled by gcp_deploy.sh.
# 1) fetch .env from Secret Manager  2) fetch the current release from the private bucket
# 3) first boot only: seed research DB + market-data cache  4) install/start the Docker stack
# 5) install a 5-minute timer that reports health to the bucket and applies new releases
set -euo pipefail
APP=/opt/quant-trader
BUCKET=gs://__BUCKET__
PROJECT=__PROJECT__
mkdir -p "$APP/data"
umask 077

gcloud secrets versions access latest --secret=qt-env --project="$PROJECT" > "$APP/.env.new"
IP=$(curl -s -H "Metadata-Flavor: Google" \
  http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/access-configs/0/external-ip)
sed -i "s/^DASHBOARD_DOMAIN=auto\$/DASHBOARD_DOMAIN=${IP//./-}.sslip.io/" "$APP/.env.new"
mv "$APP/.env.new" "$APP/.env" && chmod 600 "$APP/.env"
umask 022

REL=$(gcloud storage cat "$BUCKET/release.txt")
if [ "$(cat "$APP/release.txt" 2>/dev/null || true)" != "$REL" ] || [ ! -d "$APP/repo" ]; then
  rm -rf "$APP/repo.new" && mkdir -p "$APP/repo.new"
  gcloud storage cp "$BUCKET/releases/$REL" "$APP/rel.tar.gz"
  tar -xzf "$APP/rel.tar.gz" -C "$APP/repo.new" && rm -f "$APP/rel.tar.gz"
  rm -rf "$APP/repo.old"
  [ -d "$APP/repo" ] && mv "$APP/repo" "$APP/repo.old"
  mv "$APP/repo.new" "$APP/repo"
fi

if [ ! -f "$APP/data/qt.db" ] && gcloud storage objects describe "$BUCKET/seed/qt.db" >/dev/null 2>&1; then
  gcloud storage cp "$BUCKET/seed/qt.db" "$APP/data/qt.db"
fi
if [ ! -d "$APP/data/cache" ] && gcloud storage objects describe "$BUCKET/seed/cache.tar" >/dev/null 2>&1; then
  gcloud storage cp "$BUCKET/seed/cache.tar" "$APP/cache.tar" && tar -xf "$APP/cache.tar" -C "$APP/data" && rm -f "$APP/cache.tar"
fi

# a container stuck in the kernel can make "compose up" fail; restart Docker once and retry instead of aborting
# (aborting would skip the health timer below and leave the server unobservable)
if ! APP_DIR="$APP" DATA_DIR="$APP/data" SRC_DIR="$APP/repo" bash "$APP/repo/deploy/install.sh"; then
  echo "install failed; restarting docker and retrying once"
  systemctl restart docker || true; sleep 15
  APP_DIR="$APP" DATA_DIR="$APP/data" SRC_DIR="$APP/repo" bash "$APP/repo/deploy/install.sh" || echo "install failed twice"
fi
echo "$REL" > "$APP/release.txt"

# health reporter + self-update timer
install -m 755 "$APP/repo/deploy/vm_health.sh" /usr/local/bin/qt-health
echo "${BUCKET#gs://}" > /etc/qt-bucket   # (a sed placeholder here was itself replaced by gcp_deploy.sh)
cat > /etc/systemd/system/qt-health.service <<EOF
[Unit]
Description=quant-trader health report and release check
[Service]
Type=oneshot
Environment=HOME=/root
ExecStart=/usr/local/bin/qt-health
EOF
cat > /etc/systemd/system/qt-health.timer <<EOF
[Unit]
Description=quant-trader health every 5 minutes
[Timer]
OnBootSec=3min
OnUnitActiveSec=5min
[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now qt-health.timer
# first report right away; its text also lands in this script's output (Cloud Logging) for remote diagnosis
/usr/local/bin/qt-health || true
