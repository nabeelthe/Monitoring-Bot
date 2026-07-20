# Nuva Intelligence Platform

An AI-analyst intelligence platform for the **Nuva Labs / Nuva Finance / Provenance
Blockchain** ecosystem — not a notification bot. Every signal is scored, correlated
into cross-layer stories, risk-assessed, explained, and routed by priority; the full
history is queryable; executive reports and a live dashboard come built in.

- **HASH** = Provenance L1 token (live market proxy)
- **NUVA** = Nuva Finance token (pre-TGE; watched for TGE / airdrop / listing)
- The unrelated *nuvalab.ai* gaming startup is filtered out automatically.

```
 collectors (16 sources)          intelligence pipeline                delivery
┌───────────────────────┐   ┌──────────────────────────────┐   ┌──────────────────────┐
│ on-chain · ethereum   │   │ correlator  → stories        │   │ critical → 🚨 instant │
│ market · token/TGE    │ → │ scorer      → confidence 0-99│ → │ high     → instant   │
│ dev · blog · social   │   │ AI analyst  → why/action     │   │ medium   → digest    │
│ news · discord        │   │ risk engine → 7 dimensions   │   │ low      → reports   │
└───────────────────────┘   │ event store → SQLite memory  │   │ dashboard · /metrics │
                            └──────────────────────────────┘   └──────────────────────┘
```

## Every alert is an analyst brief

Instead of *"price moved 8%"* you get: **what happened**, **why it matters**,
**confidence %**, **assessment** (bullish/bearish/suspicious), **correlated signals**
from the same story across other layers, **risk note**, **suggested action**,
**what to monitor next**, and a **historical comparison** with similar past events.

Briefs are written by the **AI analyst** (Claude, via `ANTHROPIC_API_KEY`) for
critical/high events, with a cost guard (`max_analyses_per_hour`). Without a key the
platform falls back to deterministic rule-based briefs — it never goes dumb silently.

## The Quant Brain (v3.0)

| Engine | What it does |
|---|---|
| **Quant signals** | Momentum (1h/6h/24h/7d), RSI, EMA trend, price/volume z-scores, volatility regime (trending/ranging/turbulent) → a −100…+100 Quant Score with a per-factor plain-language breakdown |
| **Outcome tracker** | Measures what price actually did 1h/24h after every signal the bot fired → honest per-signal hit-rate tables with sample sizes ("mint events preceded a +2% day 8/11 times") |
| **Decision engine** | Blends quant score + 24h news flow + risk panel + track record into one stance: BULLISH/BEARISH/NEUTRAL with conviction (low/med/high) and the top reasons — `/quant` |

The bot keeps score on itself: predictions cite measured history, conviction is
capped until the track record grows, and every stance ends with
"data read, not financial advice."

## Intelligence engines

| Engine | What it does |
|---|---|
| **Correlation** | Events sharing signal tags (tge, mint, listing, tvl, exploit…) within a 6h window become one *story*; alerts show the story's cross-layer confirmations |
| **Confidence scoring** | Source credibility (on-chain 90 > social 45) + route-map class + escalation keywords + independent cross-layer corroboration → 0-99% |
| **Smart routing** | critical→instant loud · high→instant · medium→30-min digest · low→reports only · ignore→memory only. Quiet hours demote high→digest (critical always breaks through) |
| **Risk engine** | 7 dimensions (security, market, liquidity, governance, developer, reputation, operational), each with score, trend and recommendation |
| **Predictions** | Evidence-based probabilities: TGE/listing, governance, releases, partnerships, incidents, continued volatility — every estimate lists its evidence |
| **Historical memory** | Every event persisted to SQLite; query with `/history`, `/search`, the dashboard, or reports |
| **Executive reports** | Scheduled morning/evening briefs, daily summary, weekly intelligence — stats, top events, risk panel, predictions, action items |
| **Noise reduction** | Persistent dedupe, per-cycle alert caps, digest merging by source, keyword gates, gaming-namesake filter |

## What it watches (16 sources)

| Layer | Source | Cadence | Priority |
|---|---|---|---|
| On-chain | Provenance explorer (markers, mints/burns, nvAsset vault issuance, scope writes) | 2 min | ● Nuva denoms → ★ |
| On-chain | Provenance governance + chain upgrades (Cosmos LCD) | 30 min | ★ always |
| Ethereum | Etherscan v2 — NUVA ERC-20 mints/transfers, vault-manager txs | 3 min | ● on mint |
| Market | CoinGecko HASH (price bands, volume spikes, daily summary) | 5 min | ● big moves |
| Market | DefiLlama TVL swings | 30 min | ● large swings |
| Market | Osmosis HASH/OSMO pool (off until `pool_id` set) | 10 min | ● large swings |
| Token | Nuva pages (news/home/app), Genesis Pass portal | 10 min | ★ always |
| Token | CoinGecko/CMC NUVA listing pages (404→live = probable TGE) | 10 min | ★ always |
| Dev | GitHub `provlabs` + `provenance-io` (repos, releases, notable commits) | 20 min | ● Nuva/vault |
| Blog | provenance.io/blog, docs.nuvalabs.com, developer portal | 15 min | ■ kw-based |
| Social | X via Nitter (official + ecosystem + `$NUVA` search), Reddit, YouTube, Discord | 5-60 min | ● kw priority |
| News | Google News + CoinDesk, The Block, Cointelegraph, Decrypt, The Defiant | 15 min | ● kw priority |

**Source of truth is [`config.yaml`](config.yaml)** — endpoints, addresses, handles,
thresholds, weights, routing, quiet hours, report schedule.

## Quick start

```bash
cp .env.example .env     # TELEGRAM_BOT_TOKEN required; ANTHROPIC_API_KEY recommended
docker compose up -d --build
```

Open the bot in Telegram, send **/start** — the first chat becomes the owner.
Dashboard: **http://localhost:8088** · health: `/healthz` · Prometheus: `/metrics`.

## Commands

| | |
|---|---|
| `/intelligence` | top signals + active cross-layer stories |
| `/risk` | 7-dimension risk panel with recommendations |
| `/predict` | probability estimates with the evidence behind each |
| `/report [morning\|daily\|weekly]` | executive brief on demand |
| `/history [hours]` · `/search text` | query the event memory |
| `/digest` | flush the pending medium-priority digest now |
| `/status` `/sources` `/price` `/check name` | collector health & market |
| `/mute [min]` `/unmute` `/test` `/help` | notification control |

## Environment variables

| Var | Required | Purpose |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | **yes** | from @BotFather |
| `ANTHROPIC_API_KEY` | recommended | Claude-powered analyst briefs (else rule-based) |
| `TELEGRAM_CHAT_ID` | no | pre-register the alert chat |
| `ETHERSCAN_API_KEY` | no | activates the Ethereum/NUVA monitor |
| `COINGECKO_API_KEY` / `GITHUB_TOKEN` | no | higher rate limits |
| `DISCORD_BOT_TOKEN` | no | Discord announcements monitor |

Collectors missing credentials disable themselves cleanly and activate the moment
the value is set (`python -m nuva_bot --check-config` shows the live roster).

## Operations & reliability

- **Baseline on first poll** — existing items recorded silently; only new activity alerts
- **Persistent dedupe + event memory** — restarts never re-alert; history survives in `data/`
- **Per-source health** — 5 consecutive failures → one ⚠️ alert + backoff (up to 3× cadence) + ✅ recovery note; visible in `/status`, the dashboard, and `/metrics`
- **Rate-limit safe** — ~1 msg/s pacing, automatic 429 retry-after handling, alert-storm caps
- **Nitter failover** — X monitoring rotates mirrors and sticks with a working one
- **Observability** — `/healthz` (503 when degraded), Prometheus `/metrics` (events, alerts, per-monitor failures, risk scores) — point Grafana at it
- **Verify without deploying**: `python -m nuva_bot --check-config`, `python -m nuva_bot --once`, `pytest tests/ -q` (58 tests)

## Deploying

**Docker (recommended):** `docker compose up -d --build` — state persists in a named
volume, dashboard on :8088, auto-restart.
**systemd:** [`deploy/nuva-bot.service`](deploy/nuva-bot.service).
**Railway / Fly / Render:** deploy the Dockerfile as a worker with a volume at
`/app/data`; expose :8088 if you want the dashboard public (put auth in front).

CI runs compile + config validation + the full test suite on every push.
