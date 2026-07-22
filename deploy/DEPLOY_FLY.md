# Deploying on Fly.io

Fly builds and runs the repo's `Dockerfile` on its own servers, so the bot's
network (reaching Telegram, CoinGecko, etc.) is Fly's datacenter — not your
home Wi-Fi. You drive it with the `flyctl` CLI from any terminal.

> **Cost note (be honest):** Fly no longer has the old always-free allowance for
> new accounts. A `shared-cpu-1x` 256 MB machine + 1 GB volume runs at roughly
> **$2–3/month**. A card is required. If you want strictly $0, use the Google
> Cloud guide (`deploy/DEPLOY_GCP.md`) instead — this guide is for Fly.

---

## Part 1 — Install flyctl and sign in

**macOS** (from your normal Terminal — this talks to fly.io, not Telegram, so
the block that stopped your bot earlier doesn't apply here):
```bash
brew install flyctl        # or:  curl -L https://fly.io/install.sh | sh
fly auth signup            # opens a browser; add your card here (verification)
```
Already have an account? `fly auth login`.

## Part 2 — Get the code locally
Fly builds from the repo files, so clone it (public repo, no token needed):
```bash
git clone -b claude/telegram-bot-token-monitoring-ca7hmq https://github.com/nabeelthe/monitoring-bot
cd monitoring-bot
```

## Part 3 — Create the app
App names are global, so pick a unique one and use it everywhere below
(replace `YOUR_APP`):
```bash
fly apps create YOUR_APP
```
Then open `fly.toml` and set the first line to match:
```toml
app = "YOUR_APP"
```

## Part 4 — Create the persistent volume
This is where the event memory, quant tape and dedupe state live across
restarts. Same region as `primary_region` in fly.toml (`fra`):
```bash
fly volumes create nuva_data --region fra --size 1 -a YOUR_APP
```
(Answer "yes" to the single-volume warning — one is correct for this bot.)

## Part 5 — Set your keys as secrets
Fly injects these as environment variables; `config.yaml` reads them. Fill in
your own values (never commit real keys to the repo) and run it as one command:
```bash
fly secrets set -a YOUR_APP \
  TELEGRAM_BOT_TOKEN=<your-botfather-token> \
  ETHERSCAN_API_KEY=<your-etherscan-key> \
  COINGECKO_API_KEY=<your-coingecko-key> \
  OPENROUTER_API_KEY=<your-openrouter-key> \
  TELEGRAM_ALLOWED_CHAT_IDS=<comma-separated-admin-chat-ids>
```
`OPENROUTER_API_KEY` is optional (enables the AI copilot/analyst); omit it and
the bot uses its built-in rule-based intelligence. `ANTHROPIC_API_KEY` can be
set the same way if you prefer Claude over OpenRouter.

## Part 6 — Deploy
`--remote-only` builds on Fly's servers, so you don't need Docker installed
locally:
```bash
fly deploy --remote-only -a YOUR_APP
```
Watch the build + release logs. It's healthy once the machine reaches
`running` and the release succeeds.

## Part 7 — Verify
```bash
fly logs -a YOUR_APP
```
Look for `Nuva Intelligence Platform online`, 11 monitors, and
`AI analyst provider: openrouter…`. Then open **t.me/Nuva_token_bot**, send
`/start`, and try `/terminal` and `/quant`.

Dashboard (if you kept the `[http_service]` block): **https://YOUR_APP.fly.dev**

---

## Everyday commands
```bash
fly logs -a YOUR_APP                    # live logs
fly status -a YOUR_APP                  # machine + health
fly secrets set KEY=value -a YOUR_APP   # change a key (auto-redeploys)
fly deploy --remote-only -a YOUR_APP    # ship new code after a git pull
fly apps restart YOUR_APP               # restart
```

## Troubleshooting
- **Deploy fails / machine keeps restarting right after start** — almost always
  a bad/missing `TELEGRAM_BOT_TOKEN`. Check `fly logs`; it prints an explicit
  "Telegram rejected the bot token" line. Re-set the secret and redeploy.
- **`Out of memory` / OOM restarts** — bump memory: set `memory = "512mb"` in
  `fly.toml` and `fly deploy` again (adds ~$1–2/month).
- **Two bots running** (old Oracle VM + Fly) — fine briefly, but stop one so
  they don't compete for Telegram updates. On the Oracle VM:
  `sudo systemctl stop nuva-bot`.
