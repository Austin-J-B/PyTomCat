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
sudo install -m755 -o root -g root deploy/tomcat-sync-units.sh /usr/local/sbin/tomcat-sync-units
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

## Memory limits (why the server used to vanish)

The labeler holds roughly 10MB of decoded pixels per in-flight image operation
and `tomcat.service` had no memory cap, so when the box ran out the **kernel**
chose the victims: it killed the bot every ~60s and eventually took `sshd` and
`cloudflared` with it. The server stayed powered on, answered nothing, and only
a console power-cycle brought it back.

Two limits now bound that damage, and they only work together:

| Where | Setting | Effect |
| --- | --- | --- |
| `tomcat.service` | `MemoryHigh=2.2G` | kernel throttles and reclaims first: slow, still serving |
| `tomcat.service` | `MemoryMax=2.8G` | only this unit is killed, never sshd; the box stays reachable |
| `image_budget.py` | cgroup-aware ceiling | the decode budget follows `MemoryMax`, not host RAM |

That last row is the part that is easy to get wrong: `/proc/meminfo` reports the
**host's** RAM even inside a capped unit, so a budget sized from `MemTotal`
would sail past `MemoryMax` and be killed by the very limit meant to contain it.
`_memory_ceiling_bytes()` reads the cgroup limit first, and the budget is the
smaller of `LABELER_IMAGE_BUDGET_FRACTION` (45%) and what is left after
`LABELER_IMAGE_BUDGET_RESERVE_MB` (1600MB, the process's own baseline: torch,
the DINOv3 gallery, the labeler caches).

The values suit a CPX21 (4GB). **After rescaling the server, raise `MemoryHigh`
and `MemoryMax`** -- the decode budget follows them on its own.

Unit changes apply themselves: every `deploy.sh` run calls
`sudo /usr/local/sbin/tomcat-sync-units`, which reinstalls any unit whose repo
copy differs from what is in `/etc/systemd/system`, runs `daemon-reload`, and
lets `deploy.sh` do the restart. To confirm the caps are live after a deploy:

```bash
systemctl show tomcat -p MemoryHigh -p MemoryMax
```

To apply a unit change immediately rather than waiting for 5am, run
`/home/tomcat/deploy.sh` (a no-op for code if already up to date, but it still
syncs units), or run the sync and restart by hand:

```bash
sudo /usr/local/sbin/tomcat-sync-units && sudo systemctl restart tomcat
```

Two things the sync deliberately does **not** do:

- **It will not install a `deploy/` tree that differs from `HEAD`.** The bot can
  write its own checkout (`ReadWritePaths`), so installing unverified files
  would let a compromised bot plant a root-run unit and wait for the timer.
  A dirty `deploy/` aborts the sync with a non-zero exit instead.
- **It does not update itself.** `tomcat-sync-units` runs as root, so changes to
  `deploy/tomcat-sync-units.sh` need the `install` line from the one-time setup
  re-run by hand. Only the units it copies deploy automatically.

### Swap

There is no swap on this host, so a spike has nowhere to go but the OOM killer.
2GB turns a brief overshoot into a slow moment instead of a kill:

```bash
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
sudo sysctl -w vm.swappiness=10 && echo 'vm.swappiness=10' | sudo tee /etc/sysctl.d/99-swappiness.conf
```

## Notes

- `config.yml` and the `cloudflared` binary are gitignored and persist across
  deploys, so the unit always has them.
- After changing `UI_ALLOWED_ORIGINS` (which changes `config.yml`), restart the
  tunnel to pick it up: `sudo systemctl restart cloudflared`.
- `deploy.sh` restarts `cloudflared` after pulling so config changes take effect.
