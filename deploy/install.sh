#!/usr/bin/env bash
# One-shot, no-Docker installer for the Nuva Intelligence Platform.
# Paste this into a fresh Ubuntu/Debian VPS (works in a browser SSH console):
#
#   curl -fsSL https://raw.githubusercontent.com/nabeelthe/monitoring-bot/claude/telegram-bot-token-monitoring-ca7hmq/deploy/install.sh | sudo bash
#
# Then edit /opt/nuva/.env with your keys and run:  sudo systemctl start nuva-bot
set -euo pipefail

REPO="https://github.com/nabeelthe/monitoring-bot"
BRANCH="claude/telegram-bot-token-monitoring-ca7hmq"
APP_DIR="/opt/nuva"
SVC_USER="nuvabot"

echo "==> Installing system packages (python, venv, git)…"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git >/dev/null

echo "==> Creating service user '${SVC_USER}'…"
id -u "$SVC_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin "$SVC_USER"

echo "==> Fetching the platform into ${APP_DIR}…"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" fetch --depth 1 origin "$BRANCH"
  git -C "$APP_DIR" checkout -B "$BRANCH" "origin/$BRANCH"
else
  git clone --depth 1 --branch "$BRANCH" "$REPO" "$APP_DIR"
fi

echo "==> Building the Python environment…"
python3 -m venv "$APP_DIR/.venv"
# upgrade pip/setuptools/wheel first — fixes the sgmllib3k build quirk for feedparser
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip setuptools wheel
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

echo "==> Preparing config…"
mkdir -p "$APP_DIR/data"
if [ ! -f "$APP_DIR/.env" ]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  chmod 600 "$APP_DIR/.env"
fi
chown -R "$SVC_USER:$SVC_USER" "$APP_DIR"

echo "==> Installing the systemd service…"
cp "$APP_DIR/deploy/nuva-bot.service" /etc/systemd/system/nuva-bot.service
systemctl daemon-reload
systemctl enable nuva-bot >/dev/null 2>&1 || true

cat <<EOF

============================================================
  Installed. One step left — add your keys:

    sudo nano ${APP_DIR}/.env
      TELEGRAM_BOT_TOKEN=...        (required)
      ETHERSCAN_API_KEY=...         (optional)
      COINGECKO_API_KEY=...         (optional)
      ANTHROPIC_API_KEY=...         (optional — AI analyst)

  Then start it and watch it come online:

    sudo systemctl start nuva-bot
    sudo journalctl -u nuva-bot -f

  Open t.me/Nuva_token_bot and send /start.
  Dashboard: http://<this-server-ip>:8088
============================================================
EOF
