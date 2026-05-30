# Migrating TomCat to a new host (Hetzner CX33)

Runbook for moving the bot off the under-provisioned DigitalOcean droplet
(1 vCPU / 2 GB, actively swapping → event-loop stalls → freezes) to a
**Hetzner CX33 (2 vCPU / 8 GB / 80 GB / x86)** at ~$8.50/mo.

> Keep the old droplet running until cutover (Phase 6). Rollback = just start
> its `tomcat.service` again.

---

## What lives where (so nothing is lost)

| Thing | Source | Migrate how |
|---|---|---|
| Application code | GitHub `git@github.com:Austin-J-B/PyTomCat.git` (`main`) | `git clone` |
| Python deps | `requirements-droplet.txt` | `pip install` |
| **Secrets** — `.env`, `credentials/*` (incl. `cloudflared.json`), `~/.modal.toml`, `~/.cloudflared/` | droplet only (git-ignored) | `rsync` over SSH — **never** commit |
| Tunnel config | `config.yml` (git-ignored; `scripts/start.py` regenerates it) | `rsync` (or let start.py recreate) |
| **Runtime state** — `cache/` (≈26 GB: photo corpus, `cache/discord/` pending CV feedback, `feeding_schedule.ndjson`, logs) | droplet only | `rsync` (bulk pre-sync + delta at cutover) |
| cloudflared binary | `~/PyTomCat/cloudflared` | re-download on new host |

Modal, Discord, and Google APIs are reached **outbound** and work from any IP.
The public UI is served through the **Cloudflare tunnel**, which follows
whichever host runs `cloudflared` with the same tunnel credentials — so
**no DNS change is needed**.

---

## Environment facts (current droplet)

- Ubuntu 24.04.4 LTS, Python 3.12.3
- venv: `/home/tomcat/PyTomCat/.venv`
- Entrypoint: `scripts/start.py` (launches the bot **and** the cloudflared tunnel)
- Systemd: `tomcat.service` (+ `override.conf`), `tomcat-deploy.service`, `tomcat-deploy.timer` (5am America/Chicago)
- Deploy script: `/home/tomcat/deploy.sh`
- opencv/ultralytics need apt libs: `libgl1`, `libglib2.0-0t64`

---

## Phase 0 — provision (manual)

1. Create a **Hetzner CX33**, image **Ubuntu 24.04**, location **Ashburn or Hillsboro (US)**, attach your SSH key.
2. Record the new public IP as `$NEW`. On your machine:
   ```bash
   NEW=<new.ip.addr>
   OLD=104.248.6.23
   KEY="$HOME/.ssh/TomCat/ssh-key-2026-05-20.key"   # adjust for your shell
   ```

## Phase 1 — base system (new box, as root)

```bash
ssh root@$NEW
apt-get update && apt-get -y upgrade
apt-get -y install python3.12-venv git rsync curl libgl1 libglib2.0-0t64
# bot service account
adduser --disabled-password --gecos "" tomcat
# deploy.sh uses sudo systemctl; grant just that (tighter than the old box's blanket NOPASSWD:ALL)
cat >/etc/sudoers.d/tomcat-systemctl <<'EOF'
tomcat ALL=(ALL) NOPASSWD: /usr/bin/systemctl start tomcat, /usr/bin/systemctl stop tomcat, /usr/bin/systemctl restart tomcat
EOF
chmod 440 /etc/sudoers.d/tomcat-systemctl
# let your laptop SSH in as tomcat too
install -d -o tomcat -g tomcat -m 700 /home/tomcat/.ssh
cp /root/.ssh/authorized_keys /home/tomcat/.ssh/ && chown tomcat:tomcat /home/tomcat/.ssh/authorized_keys && chmod 600 /home/tomcat/.ssh/authorized_keys
```

## Phase 2 — code + venv (new box, as tomcat)

```bash
ssh tomcat@$NEW
cd ~
# public repo → clone over HTTPS (no deploy key needed); deploy.sh fetches origin/main
git clone https://github.com/Austin-J-B/PyTomCat.git
cd PyTomCat
python3.12 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements-droplet.txt
```
> If you prefer SSH git (matches the old box), add a deploy key to GitHub and
> `git remote set-url origin git@github.com:Austin-J-B/PyTomCat.git`.

## Phase 3 — transfer secrets + state (run from your laptop)

Bulk pre-sync the big cache **while the old bot keeps running** (no downtime):
```bash
# secrets + machine-local config (small)
for f in .env config.yml; do
  rsync -e "ssh -i $KEY" -avz tomcat@$OLD:~/PyTomCat/$f tomcat@$NEW:~/PyTomCat/ 2>/dev/null \
    || rsync -avz <(ssh -i "$KEY" tomcat@$OLD "cat ~/PyTomCat/$f") tomcat@$NEW:~/PyTomCat/$f
done
rsync -e "ssh -i $KEY" -avz tomcat@$OLD:~/PyTomCat/credentials/  tomcat@$NEW:~/PyTomCat/credentials/
rsync -e "ssh -i $KEY" -avz tomcat@$OLD:~/.cloudflared/          tomcat@$NEW:~/.cloudflared/
rsync -e "ssh -i $KEY" -avz tomcat@$OLD:~/.modal.toml            tomcat@$NEW:~/.modal.toml
# big photo/state cache (26 GB — first pass, can take a while)
rsync -e "ssh -i $KEY" -avz --partial tomcat@$OLD:~/PyTomCat/cache/ tomcat@$NEW:~/PyTomCat/cache/
```
> Simplest path: run the rsyncs **on the new box** pulling from old
> (`rsync -avz tomcat@$OLD:... ~/PyTomCat/...`) using an SSH key that can reach
> the old box. Either direction is fine.

## Phase 4 — cloudflared (new box, as tomcat)

```bash
cd ~/PyTomCat
curl -L -o cloudflared https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64
chmod +x cloudflared
# config.yml + ~/.cloudflared/<tunnel>.json came over in Phase 3.
# scripts/start.py regenerates config.yml and runs the tunnel, so no manual `tunnel run` needed.
```

## Phase 5 — systemd units (new box, as root)

```bash
# main service
cat >/etc/systemd/system/tomcat.service <<'EOF'
[Unit]
Description=TomCat Discord Bot (orchestrated via scripts/start.py)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=tomcat
Group=tomcat
WorkingDirectory=/home/tomcat/PyTomCat
ExecStart=/home/tomcat/PyTomCat/.venv/bin/python /home/tomcat/PyTomCat/scripts/start.py
Restart=always
RestartSec=10
KillSignal=SIGINT
TimeoutStopSec=15
StandardOutput=journal
StandardError=journal
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=read-only
ReadWritePaths=/home/tomcat/PyTomCat
ProtectKernelTunables=true
ProtectKernelModules=true
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

# copy deploy.sh from the old box (Phase 3) or recreate it, then:
chmod +x /home/tomcat/deploy.sh && chown tomcat:tomcat /home/tomcat/deploy.sh

# scheduled deploy (5am America/Chicago — needs systemd >= 252; 24.04 ships 255)
cat >/etc/systemd/system/tomcat-deploy.service <<'EOF'
[Unit]
Description=TomCat scheduled deploy (git pull + restart)

[Service]
Type=oneshot
User=tomcat
Group=tomcat
ExecStart=/home/tomcat/deploy.sh
StandardOutput=journal
StandardError=journal
EOF

cat >/etc/systemd/system/tomcat-deploy.timer <<'EOF'
[Unit]
Description=Run tomcat-deploy daily at 5am America/Chicago

[Timer]
OnCalendar=*-*-* 05:00:00 America/Chicago
Persistent=true
Unit=tomcat-deploy.service

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now tomcat-deploy.timer
# don't start tomcat yet — wait for cutover
```

## Phase 6 — cutover (minimize downtime)

```bash
# 1) stop the OLD bot (frees its sheet/Discord sessions, stops cache writes)
ssh -i "$KEY" tomcat@$OLD "sudo systemctl stop tomcat; pkill -f cloudflared || true"
# 2) final DELTA rsync of cache (fast — only what changed since the bulk pass)
rsync -e "ssh -i $KEY" -avz --partial tomcat@$OLD:~/PyTomCat/cache/ tomcat@$NEW:~/PyTomCat/cache/
# 3) start the NEW bot (start.py brings up the tunnel too)
ssh tomcat@$NEW "sudo systemctl start tomcat"
```

## Phase 7 — verify

```bash
ssh tomcat@$NEW "systemctl status tomcat --no-pager | head -15"
ssh tomcat@$NEW "journalctl -u tomcat -n 40 --no-pager"
```
- In Discord: send `meow` and a `TomCat, identify` with an image.
- Open the labeler UI (the Cloudflare-tunneled URL) — confirm it loads + auth works.
- Watch for the new telemetry staying quiet: `send_health_degraded`, `labeler_event_loop_lag` in `logs/machine/`.
- Confirm `free -h` shows **no swap in use** under load (the whole point).

## Phase 8 — decommission

Once stable for a day, destroy the DigitalOcean droplet to stop its billing.
Keep a final snapshot/backup first if you want a safety net.

---

## Notes / gotchas
- **No DNS change:** the Cloudflare tunnel ingress follows `cloudflared`; same tunnel creds on the new box = same public hostname.
- **Modal token** is just an API token — works from the new IP unchanged.
- **systemd timezone in `OnCalendar`** needs systemd ≥ 252; Ubuntu 24.04 ships 255. ✔
- The old box used `tomcat ALL=(ALL) NOPASSWD: ALL` — the runbook tightens this to just the three `systemctl` verbs `deploy.sh` needs.
- The code fixes (PRs #71, #74, #75) make the bot degrade gracefully under slow I/O; the RAM upgrade removes the swapping that made I/O slow. Both together close out the freeze.
