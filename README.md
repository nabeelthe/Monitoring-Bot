# Nuva Labs — Telegram Monitoring Bot

Watches **every** source on the Nuva Labs monitoring route map — on-chain and off — and
delivers alerts to Telegram with a three-tier priority system.

- **HASH** = Provenance L1 token (live market proxy)
- **NUVA** = Nuva Finance token (pre-TGE; watched for TGE / airdrop / listing)
- The unrelated *nuvalab.ai* gaming startup is filtered out automatically.

## What it watches

| Layer | Source | Catches | Cadence | Priority |
|---|---|---|---|---|
| On-chain | Provenance explorer-service REST | markers, mints/burns, nvAsset vault issuance (nuHELOC, nuYLDS), scope writes | 2 min | ● Nuva denoms → ★ |
| On-chain | Provenance governance (Cosmos LCD) | new proposals, scheduled chain upgrades | 30 min | ★ always |
| Ethereum | Etherscan v2 — NUVA contracts | ERC-20 mints & transfers, vault-manager txs | 3 min | ● on mint |
| Market | CoinGecko — HASH | price moves beyond threshold, volume spikes, daily summary | 5 min | ● big moves |
| Market | DefiLlama TVL | RWA TVL swings on chain / protocol | 30 min | ● large swings |
| Market | Osmosis HASH/OSMO pool | liquidity add / drain (off until `pool_id` set) | 10 min | ● large swings |
| Token | Nuva pages (news · home · app) | any headline / page change — TGE date, airdrop terms | 10 min | ★ always |
| Token | CoinGecko / CMC NUVA listing pages | listing page returns content ⇒ probable TGE | 10 min | ★ always |
| Token | Genesis Pass / NUVA Points portal | mint campaign & airdrop multiplier updates | 10 min | ★ always |
| Dev | GitHub `provlabs` + `provenance-io` | new repos, releases, notable commits | 20 min | ● Nuva/vault |
| Blog | provenance.io/blog · docs.nuvalabs.com · developer portal | new posts, doc updates | 15 min | ■ kw-based |
| Social | X via Nitter RSS (official + ecosystem + `$NUVA` search) | all official posts; ecosystem gated to relevant | 5 min | ● kw priority |
| Social | Reddit search RSS | posts mentioning Nuva / Provenance | 30 min | ● kw priority |
| Social | YouTube channel RSS | new official / ecosystem uploads | 60 min | ■ kw-based |
| Social | Discord (optional, needs bot token) | announcement-channel messages | 10 min | ● kw priority |
| News | Google News RSS + CoinDesk, The Block, Cointelegraph, Decrypt, The Defiant | articles matching Nuva / Provenance | 15 min | ● kw priority |

**Priority key:** ★ always loud (notification on) · ● escalates on high-signal hit · ■ keyword-gated.
**Auto-escalation keywords:** TGE, airdrop, listing, mainnet, Genesis Pass, public sale, Series A,
exploit/hack, depeg, audit, snapshot — any hit makes the alert loud 🚨.

**Source of truth is [`config.yaml`](config.yaml)** — every endpoint, address, handle, org,
channel id and threshold is edited there. Verify endpoints against live sites before relying on alerts.

## Quick start

```bash
cp .env.example .env        # paste your bot token from @BotFather
docker compose up -d --build
```

Then open your bot in Telegram and send **/start** — the first chat to do so becomes the
owner and receives all alerts. That's it.

Without Docker:

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN="123456:ABC..."
python -m nuva_bot
```

## Bot commands

| Command | Effect |
|---|---|
| `/start` | register this chat for alerts |
| `/status` | health of every monitor (last poll, errors, alerts sent) |
| `/sources` | the live route map + escalation keywords |
| `/price` | HASH price right now |
| `/check <name>` | poll one monitor immediately |
| `/mute [min]` | notifications silent (default 60 min; alerts still arrive) |
| `/unmute` | notifications back on |
| `/test` | test alert |

## Environment variables

| Var | Required | Purpose |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | **yes** | from @BotFather |
| `TELEGRAM_CHAT_ID` | no | pre-register the alert chat (else `/start`) |
| `ETHERSCAN_API_KEY` | no | activates the Ethereum/NUVA contracts monitor |
| `COINGECKO_API_KEY` | no | higher CoinGecko rate limits |
| `GITHUB_TOKEN` | no | higher GitHub API rate limits |
| `DISCORD_BOT_TOKEN` | no | activates the Discord monitor |

Monitors missing their keys/addresses disable themselves cleanly and show up in
`--check-config`; they activate the moment the value is set. NUVA ERC-20 contract
addresses go in `ethereum.token_contracts` once announced.

## Verifying & operating

```bash
python -m nuva_bot --check-config   # validate config, list enabled monitors
python -m nuva_bot --once           # poll every source once, print to stdout (no token needed)
pytest tests/ -q                    # unit tests
```

Operational behavior:

- **Baseline on first poll** — existing items are recorded silently; only *new* activity alerts.
- **Dedupe** — every item id is remembered (persisted in `data/state.json`) so restarts don't re-alert.
- **Failure alerts** — a source failing 5 polls in a row produces one ⚠️ health alert, and a ✅ on recovery; polling backs off up to 3× cadence while a source is down.
- **Rate-limit safe** — ~1 msg/sec send pacing plus automatic 429 retry-after handling.
- **Nitter failover** — X monitoring rotates through the configured Nitter mirrors and sticks with a working one.
- **Alert-storm cap** — max 15 alerts per monitor per cycle.

## Deploying

**Any box with Docker** (VPS, home server): `docker compose up -d --build` — state persists
in `./data`, container restarts automatically.

**Bare VPS with systemd:** see [`deploy/nuva-bot.service`](deploy/nuva-bot.service).

**Railway / Fly.io / Render:** deploy the Dockerfile as a background worker (no HTTP port
needed) and set the env vars. Attach a small volume mounted at `/app/data` so dedupe
state survives restarts.

CI runs compile + config validation + the full test suite on every push.
