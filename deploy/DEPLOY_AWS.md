# Deploying on AWS EC2 Free Tier (step by step, no Docker)

The bot runs on a **t2.micro / t3.micro** EC2 instance — free for 12 months
under AWS's Free Tier (750 hours/month of one micro instance, which is exactly
one instance running 24/7). AWS's network reaches Telegram, CoinGecko and
Etherscan fine, so this is a clean always-on home for the platform.

> **Honest heads-up on the card.** Creating an AWS account requires a valid
> card for identity verification (AWS puts a temporary ~$1 hold, then refunds
> it). Your card was just declined by **both Fly.io and Google Cloud**
> (error `OR_BACR2_31`), which points at the card/bank blocking international
> online charges rather than the provider. AWS may hit the same wall. If it
> does, it's not this guide — it's the card. Fixes: enable "international /
> online transactions" in your bank app, try a different card, or fall back to
> the **card-free** options (recover your Oracle login — the bot is already
> deployed there — or the GitHub Actions cron mode). Try AWS first as you
> asked; just know the likely failure mode up front.

---

## Part 0 — Repo must be public (safe — no secrets are committed; `.env` is git-ignored)
GitHub → `monitoring-bot` → **Settings** → **Danger Zone** → **Change visibility** → **Make public**.
(Skip this if you use the private-repo variant in Part 3.)

## Part 1 — Create an AWS account
1. [aws.amazon.com](https://aws.amazon.com) → **Create an AWS Account**
2. Email, account name, then billing: add your card (verification hold only).
3. Pick the **Basic support — Free** plan.
4. If the card is declined here, that's the bank block described above — stop
   and use a card-free fallback instead of fighting it.

## Part 2 — Launch the instance
1. Sign in to the **AWS Management Console** → search **EC2** → open it.
2. Top-right: pick a **Region** close to you (any works; e.g. `eu-central-1`).
3. **Launch instance**.
4. **Name:** `nuva-bot`
5. **Application and OS Images:** **Ubuntu** → **Ubuntu Server 22.04 LTS**
   (make sure the tile says **"Free tier eligible"**).
6. **Instance type:** `t2.micro` (or `t3.micro`) — both marked **Free tier eligible**.
7. **Key pair:** click **Create new key pair** → name it `nuva-key` → **RSA** →
   `.pem` → download it. (You can also connect without it via the browser — see
   Part 3 — but create one anyway; it costs nothing.)
8. **Network settings:** leave defaults. Allow SSH (already checked). If you
   want the dashboard, also check **Allow HTTP** (or add a custom rule in Part 6).
9. **Configure storage:** keep the default 8 GB (free tier allows up to 30 GB gp3).
10. **Launch instance.** Wait ~30 s until **Instance state = Running**.

## Part 3 — Connect (no key setup needed)
1. EC2 → **Instances** → select `nuva-bot` → **Connect**.
2. Tab **EC2 Instance Connect** → **Connect** → a browser terminal opens as
   user `ubuntu`. (This avoids fiddling with the `.pem` file entirely.)

## Part 4 — Install the platform
Paste this one line:
```bash
curl -fsSL "https://raw.githubusercontent.com/nabeelthe/monitoring-bot/claude/telegram-bot-token-monitoring-ca7hmq/deploy/install.sh?v=$(date +%s)" | sudo bash
```

**Private-repo variant** (use instead of the line above, and skip Part 0):
```bash
sudo git clone https://GH_TOKEN@github.com/nabeelthe/monitoring-bot /opt/nuva -b claude/telegram-bot-token-monitoring-ca7hmq
sudo bash /opt/nuva/deploy/install.sh
```

## Part 5 — Add your keys
```bash
sudo nano /opt/nuva/.env
```
Paste your keys (see chat for a ready-filled block), save **Ctrl-O, Enter**, exit **Ctrl-X**.

## Part 6 — Start and verify
```bash
sudo systemctl start nuva-bot
sudo journalctl -u nuva-bot -f
```
Watch for **"Nuva Intelligence Platform online"** and the 11 monitors. Open
**t.me/Nuva_token_bot**, send **/start**, then **/status** — every collector
should be 🟢. `Ctrl-C` exits the log view (the bot keeps running and
auto-restarts on crash or reboot).

## Part 7 — (Optional) expose the dashboard on :8088
EC2 → **Security Groups** → the group attached to `nuva-bot` → **Edit inbound
rules** → **Add rule**: Type **Custom TCP**, Port **8088**, Source
**0.0.0.0/0** → **Save**. Then visit `http://<PUBLIC_IPv4>:8088`.

---

## Everyday commands
```bash
sudo systemctl status nuva-bot
sudo systemctl restart nuva-bot   # after editing .env or config.yaml
sudo journalctl -u nuva-bot -f
```

## Keeping it truly free
- Use **only** a `t2.micro`/`t3.micro` and **one** instance — that's the 750
  free hours/month (24×31 = 744, so one instance is covered).
- Set a **Billing alert**: Billing → **Budgets** → create a $1 zero-spend
  budget so AWS emails you the moment anything would cost money.
- The Free Tier lasts **12 months** from signup. After that a micro instance is
  ~$8–9/month — move to Oracle's always-free A1 (permanent $0) before then if
  you want to stay at zero.

## Two bots running?
If the old Oracle VM is still running the bot, stop one so they don't compete
for Telegram updates. On the Oracle VM: `sudo systemctl stop nuva-bot`.
