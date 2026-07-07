#!/usr/bin/env bash
# One-shot installer for running the Nuva Intelligence Platform on macOS,
# in the background, auto-starting at login via launchd. No Docker, no server.
#
#   curl -fsSL https://raw.githubusercontent.com/nabeelthe/monitoring-bot/claude/telegram-bot-token-monitoring-ca7hmq/deploy/install_macos.sh | bash
#
# Then edit ~/nuva-bot/.env and run:  launchctl kickstart -k gui/$(id -u)/com.nuva.bot
set -euo pipefail

REPO="https://github.com/nabeelthe/monitoring-bot"
BRANCH="claude/telegram-bot-token-monitoring-ca7hmq"
APP_DIR="$HOME/nuva-bot"
PLIST_LABEL="com.nuva.bot"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"

echo "==> Checking prerequisites…"
if ! command -v brew >/dev/null 2>&1; then
  echo "Homebrew not found. Install it first from https://brew.sh, then re-run this script."
  exit 1
fi

echo "==> Installing python3 and git via Homebrew (skips if already installed)…"
brew list python@3.12 >/dev/null 2>&1 || brew install python@3.12
brew list git >/dev/null 2>&1 || brew install git

PYTHON_BIN="$(brew --prefix python@3.12)/bin/python3.12"

echo "==> Fetching the platform into ${APP_DIR}…"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" fetch --depth 1 origin "$BRANCH"
  git -C "$APP_DIR" checkout -B "$BRANCH" "origin/$BRANCH"
else
  git clone --depth 1 --branch "$BRANCH" "$REPO" "$APP_DIR"
fi

echo "==> Building the Python environment…"
"$PYTHON_BIN" -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip setuptools wheel
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

mkdir -p "$APP_DIR/data" "$APP_DIR/logs"
if [ ! -f "$APP_DIR/.env" ]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  chmod 600 "$APP_DIR/.env"
fi

echo "==> Installing the launchd background service…"
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST_PATH" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${PLIST_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${APP_DIR}/.venv/bin/python</string>
    <string>-m</string>
    <string>nuva_bot</string>
  </array>
  <key>WorkingDirectory</key><string>${APP_DIR}</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>${APP_DIR}/logs/out.log</string>
  <key>StandardErrorPath</key><string>${APP_DIR}/logs/err.log</string>
</dict>
</plist>
PLIST

launchctl unload "$PLIST_PATH" >/dev/null 2>&1 || true
launchctl load "$PLIST_PATH"

cat <<EOF

============================================================
  Installed. One step left — add your keys:

    nano ${APP_DIR}/.env
      TELEGRAM_BOT_TOKEN=...        (required)
      ETHERSCAN_API_KEY=...         (optional)
      COINGECKO_API_KEY=...         (optional)
      ANTHROPIC_API_KEY=...        (optional — AI analyst)

  Then restart the service to pick up the keys:

    launchctl kickstart -k gui/\$(id -u)/${PLIST_LABEL}

  Watch it come online:

    tail -f ${APP_DIR}/logs/out.log

  Open t.me/Nuva_token_bot and send /start.
  Dashboard: http://localhost:8088

  IMPORTANT — this only runs while your Mac is awake and this
  user is logged in. In System Settings > Lock Screen (or Energy),
  set "prevent automatic sleeping when the display is off" and
  keep the Mac plugged in for 24/7 uptime. Closing the lid sleeps
  it unless it's connected to power with an external display/
  keyboard attached (clamshell mode).
============================================================
EOF
