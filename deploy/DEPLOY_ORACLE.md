# Deploying on Oracle Cloud "Always Free" (step by step, no Docker)

Oracle Cloud Shell (`nabeelkhay@cloudshell`) is **not** your server — it's a
temporary terminal that has no `sudo` and gets wiped when idle. The bot must run
on a **Compute Instance (a real VM)**. We use Cloud Shell only to create a key
and to SSH into that VM.

---

## Part 0 — Make the repo public (1 minute, safe)

The installer downloads code from GitHub. While the repo is **private** that
download fails. There are **no secrets in the repo** — your `.env` (tokens) is
git-ignored and was never committed — so making it public is safe.

- GitHub → your `monitoring-bot` repo → **Settings** → scroll to **Danger Zone**
  → **Change visibility** → **Make public**.

(Prefer to keep it private? Skip this and use the token-clone variant in Part 6.)

---

## Part 1 — Make an SSH key (in Cloud Shell)

In the Cloud Shell terminal you already have open, run:

```bash
ssh-keygen -t rsa -b 2048 -f ~/.ssh/nuva -N ""
cat ~/.ssh/nuva.pub
```

Copy the whole line it prints (starts with `ssh-rsa …`). You'll paste it next.

---

## Part 2 — Create the VM

1. Top-left **☰ menu → Compute → Instances → Create instance**
2. **Name:** `nuva-bot`
3. **Image and shape → Edit:**
   - **Image → Change image →** pick **Canonical Ubuntu 22.04** → Select
   - **Shape → Change shape →** pick a shape marked **"Always Free eligible"**
     (the AMD **VM.Standard.E2.1.Micro** is the most reliable — ARM shapes are
     often "out of capacity") → Select
4. **Add SSH keys →** choose **Paste public keys** → paste the `ssh-rsa …` line
   from Part 1
5. Click **Create**. Wait until the instance state is **RUNNING**, then copy its
   **Public IP address**.

---

## Part 3 — Connect to the VM (from Cloud Shell)

Back in Cloud Shell (replace `<PUBLIC_IP>` with your instance's IP):

```bash
ssh -i ~/.ssh/nuva ubuntu@<PUBLIC_IP>
```

Type `yes` if it asks about authenticity. Your prompt should change to
`ubuntu@nuva-bot` — now you're **on the server**.

---

## Part 4 — Install the platform (on the VM)

```bash
curl -fsSL https://raw.githubusercontent.com/nabeelthe/monitoring-bot/claude/telegram-bot-token-monitoring-ca7hmq/deploy/install.sh | sudo bash
```

**Private-repo variant** (only if you skipped Part 0) — replace `GH_TOKEN` with a
GitHub token that can read the repo:

```bash
sudo git clone https://GH_TOKEN@github.com/nabeelthe/monitoring-bot /opt/nuva -b claude/telegram-bot-token-monitoring-ca7hmq
sudo bash /opt/nuva/deploy/install.sh
```

---

## Part 5 — Add your keys and start it (on the VM)

```bash
sudo nano /opt/nuva/.env
```

Fill in (arrow-key down to each line, paste the value):

```
TELEGRAM_BOT_TOKEN=your-botfather-token
ETHERSCAN_API_KEY=your-etherscan-key
COINGECKO_API_KEY=your-coingecko-key
ANTHROPIC_API_KEY=            # optional — enables AI analyst briefs
```

Save with **Ctrl-O, Enter**, exit with **Ctrl-X**. Then:

```bash
sudo systemctl start nuva-bot
sudo journalctl -u nuva-bot -f
```

You should see "Nuva Intelligence Platform online". Open **t.me/Nuva_token_bot**,
send **/start**, then **/status** — every collector should be green.
Press **Ctrl-C** to stop watching logs (the bot keeps running).

---

## Part 6 — (Optional) expose the dashboard on port 8088

1. Instance page → **Virtual Cloud Network** → **Security Lists** → default list
   → **Add Ingress Rule:** Source `0.0.0.0/0`, IP Protocol **TCP**, Destination
   port **8088** → Add.
2. On the VM: `sudo ufw allow 8088 2>/dev/null || true`
3. Visit `http://<PUBLIC_IP>:8088`.

---

## Everyday commands (on the VM)

```bash
sudo systemctl status nuva-bot     # is it running?
sudo systemctl restart nuva-bot    # after editing .env or config.yaml
sudo journalctl -u nuva-bot -f     # live logs
```

It auto-starts on reboot and auto-restarts on crash. Done.
