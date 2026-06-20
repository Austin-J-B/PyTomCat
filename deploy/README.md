# Deployment artifacts (production host)

Infra for the Hetzner production host. The bot and tunnel run as **two
independent systemd units** so a tunnel crash can no longer take the public
site dark while the bot stays up (the old failure mode: `scripts/start.py`
spawned `cloudflared` as an unsupervised child and only waited on the bot, so a
tunnel crash produced a Cloudflare **1033** with nothing to restart it).

| Unit | Runs | Owns |
| --- | --- | --- |
| `tomcat.service` | `scripts/start.py` → bot on `127.0.0.1:8080` (and regenerates `config.yml`) | Discord bot / API |
| `cloudflared.service` | `cloudflared tunnel --config config.yml run` | Public tunnel (`ui.catsofuta.org` → `:8080`) |

`scripts/start.py` only spawns the tunnel itself on **Windows dev** machines
(no systemd there); on Linux it just keeps `config.yml` current for the unit.

## One-time install (run as root)

```bash
cd /home/tomcat/PyTomCat
sudo install -m644 -o root -g root deploy/cloudflared.service /etc/systemd/system/cloudflared.service
sudo install -m440 -o root -g root deploy/tomcat-cloudflared.sudoers /etc/sudoers.d/tomcat-cloudflared
sudo visudo -cf /etc/sudoers.d/tomcat-cloudflared   # validate sudoers syntax
sudo systemctl daemon-reload
sudo systemctl enable --now cloudflared.service
```

Then restart the bot so `start.py` stops spawning its own (Linux) tunnel child:

```bash
sudo systemctl restart tomcat
```

## Notes

- `config.yml` and the `cloudflared` binary are gitignored and persist across
  `git reset --hard` deploys, so the unit always has them.
- After changing `UI_ALLOWED_ORIGINS` (which changes `config.yml`), restart the
  tunnel to pick it up: `sudo systemctl restart cloudflared`.
- `/home/tomcat/deploy.sh` (the daily 5am deploy, not in this repo) restarts
  `cloudflared` after pulling so config changes take effect.
