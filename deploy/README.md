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
sudo install -m644 -o root -g root deploy/tomcat.service /etc/systemd/system/tomcat.service
sudo install -m644 -o root -g root deploy/tomcat-deploy.service /etc/systemd/system/tomcat-deploy.service
sudo install -m644 -o root -g root deploy/tomcat-deploy.timer /etc/systemd/system/tomcat-deploy.timer
sudo install -m440 -o root -g root deploy/tomcat-cloudflared.sudoers /etc/sudoers.d/tomcat-cloudflared
sudo visudo -cf /etc/sudoers.d/tomcat-cloudflared   # validate sudoers syntax
sudo systemctl daemon-reload
sudo systemctl enable --now cloudflared.service
sudo systemctl enable --now tomcat.service tomcat-deploy.timer
```

Then restart the bot so `start.py` stops spawning its own (Linux) tunnel child:

```bash
sudo systemctl restart tomcat
```

## Deploys and runtime state

`deploy.sh` in this directory is the daily 5am deploy, fired by
`tomcat-deploy.timer`. Install it with:

```bash
cp deploy/deploy.sh /home/tomcat/deploy.sh && chmod +x /home/tomcat/deploy.sh
```

It updates with `git merge --ff-only`, which **refuses** when the working tree
cannot cleanly fast-forward. It must never go back to `git reset --hard`: the
host holds live runtime state in the working tree, and a hard reset silently
restores whatever was last committed over it.

Files the bot writes continuously are gitignored and must stay that way:

| Path | Backed up by |
| --- | --- |
| `TomCatBot Pics.csv` | `TomCatBot Pics` worksheet, mirrored every ~5 min |
| `cache/catabase/Catabase - CatDatabase.csv` | snapshot of the CatDatabase sheet, rebuilt on boot |
| `cache/catabase/profiles.json` | derived from the catabase CSV, rebuilt on boot |
| `cache/feeding_checklist.ndjson` | pre-update tarball in `deploy.sh` |
| `cache/feeding_schedule.ndjson` | pre-update tarball in `deploy.sh` |

Tarballs land in `/home/tomcat/backups/runtime/`, last 14 kept.

The worksheet mirror is destructive (clear + rewrite), so `sync_metadata_csv_to_sheet`
refuses to shrink the sheet by more than `PHOTO_METADATA_SHEET_SYNC_MAX_SHRINK`
(default 5%). Without that, a truncated local CSV would be copied over the only
backup on the next sync.

## Notes

- `config.yml` and the `cloudflared` binary are gitignored and persist across
  deploys, so the unit always has them.
- After changing `UI_ALLOWED_ORIGINS` (which changes `config.yml`), restart the
  tunnel to pick it up: `sudo systemctl restart cloudflared`.
- `deploy.sh` restarts `cloudflared` after pulling so config changes take effect.
