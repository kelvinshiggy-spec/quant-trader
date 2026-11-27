#!/usr/bin/env bash
# One-shot server setup for Debian 12 / Ubuntu 22.04+ (run as root). Idempotent.
#   - installs Docker, creates swap, clones/updates the repo, starts the stack
#   - Docker restarts every service on crash and on reboot; autoheal restarts unhealthy ones
# Usage: REPO_URL=... REPO_REF=main bash install.sh   (.env must already exist in /opt/quant-trader)
#    or: SRC_DIR=/opt/quant-trader/repo DATA_DIR=/opt/quant-trader/data bash install.sh   (code already unpacked,
#        as done by the Google Cloud boot script; no git or GitHub token needed)
set -euo pipefail
APP="${APP_DIR:-/opt/quant-trader}"
SRC_DIR="${SRC_DIR:-}"
DATA_DIR="${DATA_DIR:-}"
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
if [ -z "$SRC_DIR" ]; then
  REPO_URL="${REPO_URL:?set REPO_URL or SRC_DIR}"
  if [ ! -d "$APP/repo/.git" ]; then
    git clone --branch "$REPO_REF" "$REPO_URL" "$APP/repo"
  else
    git -C "$APP/repo" fetch origin "$REPO_REF" && git -C "$APP/repo" checkout -q FETCH_HEAD
  fi
fi
[ -f "$APP/.env" ] || { echo "missing $APP/.env"; exit 1; }
chmod 600 "$APP/.env"
ln -sf "$APP/.env" "$APP/repo/.env"
if [ -n "$DATA_DIR" ]; then
  # data lives outside the code directory so releases can be replaced without touching the database
  mkdir -p "$DATA_DIR"
  [ -d "$APP/repo/data" ] && [ ! -L "$APP/repo/data" ] && rm -rf "$APP/repo/data"
  ln -sfn "$DATA_DIR" "$APP/repo/data"
  chown -R 10001:10001 "$DATA_DIR"
else
  mkdir -p "$APP/repo/data" && chown -R 10001:10001 "$APP/repo/data"
fi

cd "$APP/repo"
docker compose up -d --build
docker compose ps
