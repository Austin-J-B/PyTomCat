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

Systemd units and sudoers policy are privileged configuration. An administrator
must review them and install from a **root-controlled trusted copy** (for
example, a checkout under `/root` whose revision was reviewed independently).
Never install these files directly from `/home/tomcat/PyTomCat`: the `tomcat`
service can write that checkout. The sync helper is deprecated and fails
closed.

```bash
TRUSTED_DEPLOY_DIR=/root/reviewed-PyTomCat/deploy
sudo install -m644 -o root -g root "$TRUSTED_DEPLOY_DIR/cloudflared.service" /etc/systemd/system/cloudflared.service
sudo install -m644 -o root -g root "$TRUSTED_DEPLOY_DIR/tomcat.service" /etc/systemd/system/tomcat.service
sudo install -m644 -o root -g root "$TRUSTED_DEPLOY_DIR/tomcat-deploy.service" /etc/systemd/system/tomcat-deploy.service
sudo install -m644 -o root -g root "$TRUSTED_DEPLOY_DIR/tomcat-deploy.timer" /etc/systemd/system/tomcat-deploy.timer
sudo install -m440 -o root -g root "$TRUSTED_DEPLOY_DIR/tomcat-cloudflared.sudoers" /etc/sudoers.d/tomcat-cloudflared
sudo visudo -cf /etc/sudoers.d/tomcat-cloudflared
sudo systemctl daemon-reload
sudo systemctl enable --now cloudflared.service
sudo systemctl enable --now tomcat.service tomcat-deploy.timer
```

### Urgent production migration from automatic unit sync

The previous `tomcat-sync-units` helper installed units and sudoers policy from
the bot-writable checkout. Remove that path during migration:

1. From a root-controlled trusted copy, review and install the updated deploy
   script without the helper call:

   ```bash
   TRUSTED_DEPLOY_DIR=/root/reviewed-PyTomCat/deploy
   sudo install -m755 -o tomcat -g tomcat "$TRUSTED_DEPLOY_DIR/deploy.sh" /home/tomcat/deploy.sh
   ```

2. Review and manually install all four unit files and the narrowed sudoers
   file using the commands above.
3. Validate the installed sudoers policy with `visudo -cf` as shown, then run
   `sudo systemctl daemon-reload`.
4. Confirm the helper is no longer referenced by the active sudoers policy or
   deployment configuration, then remove the old root-owned helper:

   ```bash
   sudo grep -R -n --fixed-strings '/usr/local/sbin/tomcat-sync-units' /etc/sudoers /etc/sudoers.d /home/tomcat/deploy.sh /etc/systemd/system || true
   sudo rm -f /usr/local/sbin/tomcat-sync-units
   ```

   If the reference check finds any active configuration reference, remove it
   and confirm again before deleting the helper.

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

### One-time migration for tracked sensitive logs

**Before deploying the commit that removes the tracked `logs/subs/` and
`logs/moderation/` files, preserve the production data using this procedure.**
The existing tracked files make `git merge --ff-only` reject the update when
they contain live changes. The old deploy script cannot automate this
transition safely.

Stop TomCat first so the log files cannot change while they are copied. The
archive below is stored outside the checkout in a root-only directory; keep it
until the restored runtime logs have been verified and backed up elsewhere.

```bash
REPO_DIR=/home/tomcat/PyTomCat
BACKUP_DIR=/home/tomcat/backups/pre-sensitive-log-ignore-$(date +%Y%m%d-%H%M%S)
sudo systemctl stop tomcat tomcat-deploy.timer tomcat-deploy.service
sudo install -d -o root -g root -m700 "$BACKUP_DIR"
sudo tar -czf "$BACKUP_DIR/sensitive-logs.tgz" -C "$REPO_DIR" logs/subs logs/moderation
sudo chmod 600 "$BACKUP_DIR/sensitive-logs.tgz"
sudo tar -tzf "$BACKUP_DIR/sensitive-logs.tgz"
```

Review the archive listing to confirm it contains only the expected log paths.
Restore the checkout copies to their current committed bytes so the fast-forward
can apply the tracked deletions, then run the deploy script that was updated
from a trusted copy as described in the urgent unit-sync migration above:

```bash
sudo -u tomcat git -C "$REPO_DIR" restore --source=HEAD --worktree -- logs/subs logs/moderation
sudo -u tomcat /home/tomcat/deploy.sh
sudo systemctl stop tomcat
```

After the new ignore rules and commit are present, restore the preserved runtime
logs and verify ownership and restrictive permissions before restarting TomCat:

```bash
sudo tar -xzf "$BACKUP_DIR/sensitive-logs.tgz" -C "$REPO_DIR"
sudo chown -R tomcat:tomcat "$REPO_DIR/logs/subs" "$REPO_DIR/logs/moderation"
sudo find "$REPO_DIR/logs/subs" "$REPO_DIR/logs/moderation" -type d -exec chmod 700 {} +
sudo find "$REPO_DIR/logs/subs" "$REPO_DIR/logs/moderation" -type f -exec chmod 600 {} +
sudo find "$REPO_DIR/logs/subs" "$REPO_DIR/logs/moderation" -printf '%u:%g %m %p\n'
```

Confirm each file is owned by `tomcat:tomcat`, directories are mode `700`, and
files are mode `600`. Keep the protected archive; do not discard it as part of
this migration.

```bash
sudo systemctl start tomcat
sudo systemctl start tomcat-deploy.timer
```

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

Code deploys do not install systemd units or sudoers files. Unit and sudoers
changes require explicit administrator review and installation from a
root-controlled trusted copy, followed by `daemon-reload` and any needed
service restart. To confirm the caps are live after a deploy:

```bash
systemctl show tomcat -p MemoryHigh -p MemoryMax
```

To apply a unit change immediately, have an administrator install the reviewed
unit from the trusted copy, then reload systemd and restart the affected unit:

```bash
sudo systemctl daemon-reload && sudo systemctl restart tomcat
```

The deprecated `tomcat-sync-units` script now exits with an error and performs
no privileged actions. Do not install or invoke it.

## Encrypted off-site backups

`ccc-backup` creates `age`-encrypted archives locally and uploads only the
ciphertext and its SHA-256 sidecar to the `ccc-drive` rclone remote. Essential
runtime and recovery state is backed up daily; model weights are backed up
weekly. Large, reproducible image caches are intentionally excluded.

The production host must not hold a decryption key. Put only public recipients
in `/etc/ccc-backup/recipients.txt`, with at least two independently controlled
officer recovery keys. The rclone OAuth configuration is a secret and must stay
root-only at `/root/.config/rclone/rclone.conf`; never copy it into Git.

After reviewing these files from a root-controlled trusted checkout, install
them with:

```bash
TRUSTED_DEPLOY_DIR=/root/reviewed-PyTomCat/deploy
sudo install -m755 -o root -g root "$TRUSTED_DEPLOY_DIR/ccc-backup" /usr/local/sbin/ccc-backup
sudo install -d -m755 -o root -g root /etc/ccc-backup
# Create recipients.txt manually from reviewed public keys; do not install the
# example file unchanged.
sudo install -m644 -o root -g root "$TRUSTED_DEPLOY_DIR/ccc-backup-essential.service" /etc/systemd/system/
sudo install -m644 -o root -g root "$TRUSTED_DEPLOY_DIR/ccc-backup-essential.timer" /etc/systemd/system/
sudo install -m644 -o root -g root "$TRUSTED_DEPLOY_DIR/ccc-backup-models.service" /etc/systemd/system/
sudo install -m644 -o root -g root "$TRUSTED_DEPLOY_DIR/ccc-backup-models.timer" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ccc-backup-essential.timer ccc-backup-models.timer
```

Validate a manual run and confirm `uploaded=yes` before relying on the timers:

```bash
sudo systemctl start ccc-backup-essential.service
sudo systemctl show ccc-backup-essential.service -p Result -p ExecMainStatus
sudo journalctl -t ccc-backup -n 5 --no-pager
```

Local ciphertext older than seven days is removed after a successful run.
Remote archives are deliberately not deleted by the server: a compromised host
should not automatically erase the recovery history. Review Drive retention
and perform a test restore with an officer-held key at least once per semester.

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
