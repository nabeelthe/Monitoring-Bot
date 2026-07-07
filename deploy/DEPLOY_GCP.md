# Deploying on Google Cloud "Always Free" `e2-micro` (step by step, no Docker)

Google Cloud Shell is a temporary terminal (no persistence) — used only to
create/manage the VM. The bot runs on the **e2-micro Compute Engine VM**, which
is free forever under GCP's Always Free tier (one instance in `us-west1`,
`us-central1`, or `us-east1`).

---

## Part 0 — Repo must be public (safe — no secrets are committed; `.env` is git-ignored)
GitHub → `monitoring-bot` → **Settings** → **Danger Zone** → **Change visibility** → **Make public**.

## Part 1 — Create the VM
1. [console.cloud.google.com](https://console.cloud.google.com) → create/select a project
2. **☰ menu → Compute Engine → VM instances → Create instance**
3. **Name:** `nuva-bot`
4. **Region:** `us-west1`, `us-central1`, or `us-east1` (only these qualify free) — any zone
5. **Machine type:** `e2-micro`
6. **Boot disk → Change:** Ubuntu → **Ubuntu 22.04 LTS**, size ≤30GB (free tier limit)
7. Leave everything else default → **Create**

## Part 2 — Connect
On the VM instances list, click the **SSH** button next to `nuva-bot` — opens a
browser terminal directly, no key setup needed.

## Part 3 — Install the platform
```bash
curl -fsSL https://raw.githubusercontent.com/nabeelthe/monitoring-bot/claude/telegram-bot-token-monitoring-ca7hmq/deploy/install.sh | sudo bash
```

**Private-repo variant** (skip Part 0 instead):
```bash
sudo git clone https://GH_TOKEN@github.com/nabeelthe/monitoring-bot /opt/nuva -b claude/telegram-bot-token-monitoring-ca7hmq
sudo bash /opt/nuva/deploy/install.sh
```

## Part 4 — Add your keys
```bash
sudo nano /opt/nuva/.env
```
Paste your keys (see chat for a ready-filled block), save **Ctrl-O, Enter**, exit **Ctrl-X**.

## Part 5 — Start and verify
```bash
sudo systemctl start nuva-bot
sudo journalctl -u nuva-bot -f
```
Watch for "Nuva Intelligence Platform online". Open your bot in Telegram, send
**/start**, then **/status** — every collector should be 🟢. Ctrl-C exits the
log view (bot keeps running, auto-restarts on crash/reboot).

## Part 6 — (Optional) expose the dashboard on :8088
**VPC network → Firewall → Create firewall rule:**
Name `allow-8088`, Targets: All instances, Source ranges `0.0.0.0/0`,
Protocols/ports: `tcp:8088` → Create.
Then visit `http://<VM_EXTERNAL_IP>:8088`.

---

## Everyday commands
```bash
sudo systemctl status nuva-bot
sudo systemctl restart nuva-bot   # after editing .env or config.yaml
sudo journalctl -u nuva-bot -f
```
