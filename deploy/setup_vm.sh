#!/usr/bin/env bash
# =============================================================================
# One-shot provisioning script for a fresh Oracle Cloud Ubuntu VM.
#
# It installs system packages, creates the Python virtualenv, installs the app
# dependencies, and installs + starts the systemd services and nginx proxy.
#
# Run it from the repo root on the VM:
#     cd ~/Stonks
#     chmod +x deploy/setup_vm.sh
#     ./deploy/setup_vm.sh
#
# BEFORE running, make sure you have created your .env file:
#     cp .env.example .env && nano .env      # add your Alpaca PAPER keys
#
# This script is safe to re-run; it is idempotent where practical.
# =============================================================================
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_USER="$(id -un)"
cd "$REPO_DIR"

echo "==> Repo:  $REPO_DIR"
echo "==> User:  $SERVICE_USER"

if [[ ! -f .env ]]; then
  echo "!! No .env file found. Copy .env.example to .env and add your Alpaca keys first."
  echo "   cp .env.example .env && nano .env"
  exit 1
fi

echo "==> Installing system packages (python, nginx, tooling)..."
sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip nginx apache2-utils

echo "==> Creating Python virtualenv (.venv) and installing dependencies..."
python3 -m venv .venv
./.venv/bin/pip install --upgrade pip
./.venv/bin/pip install -r requirements.txt

echo "==> Installing systemd services..."
# Render the unit files with the correct user + repo path, then install them.
for unit in stonks-bot stonks-dashboard; do
  sed -e "s#/home/ubuntu/Stonks#${REPO_DIR}#g" \
      -e "s#^User=ubuntu#User=${SERVICE_USER}#" \
      "deploy/${unit}.service" | sudo tee "/etc/systemd/system/${unit}.service" >/dev/null
done
sudo systemctl daemon-reload
sudo systemctl enable --now stonks-bot stonks-dashboard

echo "==> Configuring nginx reverse proxy..."
sudo cp deploy/nginx-stonks.conf /etc/nginx/sites-available/stonks
sudo ln -sf /etc/nginx/sites-available/stonks /etc/nginx/sites-enabled/stonks
sudo rm -f /etc/nginx/sites-enabled/default || true

if [[ ! -f /etc/nginx/.stonks_htpasswd ]]; then
  echo "==> Create the dashboard login now."
  read -r -p "    Dashboard username: " DASH_USER
  sudo htpasswd -c /etc/nginx/.stonks_htpasswd "$DASH_USER"
fi

sudo nginx -t
sudo systemctl reload nginx

echo "==> Opening port 80 in the VM firewall (iptables)..."
# Oracle Ubuntu images ship with a restrictive iptables INPUT chain. Add an
# ACCEPT for port 80 before the default REJECT rule if it is not already there.
if ! sudo iptables -C INPUT -p tcp --dport 80 -j ACCEPT 2>/dev/null; then
  sudo iptables -I INPUT 6 -p tcp --dport 80 -j ACCEPT
  # Persist across reboots.
  sudo apt-get install -y iptables-persistent
  sudo netfilter-persistent save
fi

echo
echo "============================================================"
echo " Setup complete."
echo
echo " Bot status:        sudo systemctl status stonks-bot"
echo " Dashboard status:  sudo systemctl status stonks-dashboard"
echo " Bot logs:          journalctl -u stonks-bot -f"
echo
echo " IMPORTANT: you still must open port 80 in the Oracle Cloud"
echo " console (VCN Security List / Network Security Group ingress)."
echo " See docs/ORACLE_SETUP.md, step 5."
echo
PUBLIC_IP="$(curl -s ifconfig.me || echo YOUR_VM_PUBLIC_IP)"
echo " Then browse to:  http://${PUBLIC_IP}/"
echo "============================================================"
