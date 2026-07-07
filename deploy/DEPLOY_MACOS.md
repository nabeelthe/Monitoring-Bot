# Running the Nuva Intelligence Platform on your own Mac (free, no cloud, no card)

Runs as a background service via macOS `launchd` — starts automatically when
you log in, restarts itself if it crashes. No Docker, no server, no signup.

**Trade-off vs a cloud VM:** this only runs while your Mac is powered on,
awake, and logged in. See the sleep-prevention note at the end.

---

## Part 0 — Repo must be public (safe — no secrets are committed; `.env` is git-ignored)
GitHub → `monitoring-bot` → **Settings** → **Danger Zone** → **Change visibility** → **Make public**.

*(Prefer to keep it private? Use the token-clone command in Part 2 instead of Part 1's one-liner.)*

## Part 1 — Install (paste in Terminal)

If you don't have [Homebrew](https://brew.sh) yet, install it first — the
script will tell you if it's missing.

```bash
curl -fsSL https://raw.githubusercontent.com/nabeelthe/monitoring-bot/claude/telegram-bot-token-monitoring-ca7hmq/deploy/install_macos.sh | bash
```

This installs Python 3.12 + git via Homebrew (if not already present), clones
the platform to `~/nuva-bot`, builds a virtual environment, and registers a
`launchd` background service.

## Part 2 — Private-repo variant (skip if you did Part 0)

```bash
git clone https://GH_TOKEN@github.com/nabeelthe/monitoring-bot ~/nuva-bot -b claude/telegram-bot-token-monitoring-ca7hmq
bash ~/nuva-bot/deploy/install_macos.sh
```
(replace `GH_TOKEN` with a GitHub personal access token that can read the repo)

## Part 3 — Add your keys

```bash
nano ~/nuva-bot/.env
```

Fill in:
```
TELEGRAM_BOT_TOKEN=your-botfather-token
ETHERSCAN_API_KEY=your-etherscan-key
COINGECKO_API_KEY=your-coingecko-key
ANTHROPIC_API_KEY=            # optional — enables AI analyst briefs
```

Save: **Ctrl-O, Enter**. Exit: **Ctrl-X**.

## Part 4 — Start it and verify

```bash
launchctl kickstart -k gui/$(id -u)/com.nuva.bot
tail -f ~/nuva-bot/logs/out.log
```

Watch for "Nuva Intelligence Platform online". Open **t.me/Nuva_token_bot**,
send **/start**, then **/status** — every collector should show 🟢.
Ctrl-C stops watching logs (the bot keeps running in the background).
Dashboard: **http://localhost:8088**

---

## Keep it running 24/7

macOS sleeps the machine when idle or when the lid closes, which pauses the
bot. To keep it truly always-on:

1. **System Settings → Lock Screen** (or **Battery/Energy** on older macOS)
   → turn on **"Prevent automatic sleeping when the display is off"** (while
   plugged into power).
2. Keep the Mac **plugged into power** at all times.
3. If it's a laptop, either **leave the lid open**, or connect an external
   display + keyboard/mouse so it stays awake in **clamshell mode** with the
   lid closed.

## Everyday commands

```bash
launchctl kickstart -k gui/$(id -u)/com.nuva.bot   # restart (after editing .env/config.yaml)
launchctl print gui/$(id -u)/com.nuva.bot           # is it loaded/running?
tail -f ~/nuva-bot/logs/out.log                     # live logs
tail -f ~/nuva-bot/logs/err.log                     # errors, if any
launchctl unload ~/Library/LaunchAgents/com.nuva.bot.plist   # stop it entirely
```
