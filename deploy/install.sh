#!/usr/bin/env bash
# One-shot server setup for Debian 12 / Ubuntu 22.04+ (run as root). Idempotent.
#   - installs Docker, creates swap, clones/updates the repo, starts the stack
#   - Docker restarts every service on crash and on reboot; autoheal restarts unhealthy ones
# Usage: REPO_URL=... REPO_REF=main bash install.sh   (.env must already exist in /opt/quant-trader)
set -euo pipefail
APP=/opt/quant-trader
REPO_URL="${REPO_URL:?set REPO_URL}"
REPO_REF="${REPO_REF:-main}"
export DEBIAN_FRONTEND=noninteractive

if ! command -v docker >/dev/null; then
  apt-get update -y && apt-get install -y ca-certificates curl git
  curl -fsSL https://get.docker.com | sh
fi
systemctl enable --now docker

# 2 GB swap so nightly research fits on small (1 GB) machines
if ! swapon --show | grep -q /swapfile; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  grep -q /swapfile /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# keep the clock accurate (the engine refuses to trade if it drifts > 10 s from the broker)
timedatectl set-ntp true || true

mkdir -p "$APP"
if [ ! -d "$APP/repo/.git" ]; then
  git clone --branch "$REPO_REF" "$REPO_URL" "$APP/repo"
else
  git -C "$APP/repo" fetch origin "$REPO_REF" && git -C "$APP/repo" checkout -q FETCH_HEAD
fi
[ -f "$APP/.env" ] || { echo "missing $APP/.env"; exit 1; }
chmod 600 "$APP/.env"
ln -sf "$APP/.env" "$APP/repo/.env"
mkdir -p "$APP/repo/data" && chown -R 10001:10001 "$APP/repo/data"

cd "$APP/repo"
docker compose up -d --build
docker compose ps
