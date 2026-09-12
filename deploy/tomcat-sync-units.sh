#!/usr/bin/env bash
# Install systemd units from the repo so unit changes on main take effect
# without a manual step. Run as root; deploy.sh invokes it through a single
# narrowly-scoped sudoers entry (deploy/tomcat-cloudflared.sudoers).
#
# Install:  sudo install -m755 -o root -g root deploy/tomcat-sync-units.sh \
#               /usr/local/sbin/tomcat-sync-units
#
# This script is deliberately NOT self-updating. It runs as root, so pulling a
# new copy of itself out of a tree the bot can write would hand the bot root.
# Changes to THIS file need the install line above re-run by hand; changes to
# the unit files it copies are what deploy automatically.
#
# Exiting 0 with nothing installed is the normal, boring case.

set -euo pipefail

REPO_DIR=/home/tomcat/PyTomCat
UNIT_DIR=/etc/systemd/system
SUDOERS=/etc/sudoers.d/tomcat-cloudflared

UNITS=(tomcat.service cloudflared.service tomcat-deploy.service tomcat-deploy.timer)

cd "$REPO_DIR"

# These files get installed as root-run systemd units, and the bot can write
# this tree (tomcat.service grants ReadWritePaths=$REPO_DIR). Installing
# whatever happens to be on disk would therefore let a compromised bot plant a
# unit and wait for the 5am timer. Refuse unless the files match the commit
# they claim to be: a tampered working copy differs from HEAD and stops here.
# safe.directory because we are root operating on tomcat's checkout.
if ! git -c safe.directory="$REPO_DIR" diff --quiet HEAD -- deploy; then
  echo "[sync-units] ERROR: deploy/ differs from HEAD; refusing to install." >&2
  echo "[sync-units] Inspect with: git -C $REPO_DIR diff HEAD -- deploy" >&2
  exit 1
fi

CHANGED=()

for unit in "${UNITS[@]}"; do
  src="deploy/$unit"
  dst="$UNIT_DIR/$unit"
  if [ ! -f "$src" ]; then
    echo "[sync-units] WARNING: $src missing from repo, skipping" >&2
    continue
  fi
  if ! cmp -s "$src" "$dst"; then
    install -m644 -o root -g root "$src" "$dst"
    CHANGED+=("$unit")
    echo "[sync-units] installed $unit"
  fi
done

# The sudoers file gates this script's own privilege, so a bad edit could lock
# deploys out entirely. Validate before keeping it, and roll back if invalid.
if ! cmp -s deploy/tomcat-cloudflared.sudoers "$SUDOERS"; then
  # Bare `[ -f x ] && cp` would abort the script under `set -e` when the file
  # does not exist yet, so branch explicitly.
  backup=$(mktemp)
  if [ -f "$SUDOERS" ]; then
    cp "$SUDOERS" "$backup"
  fi
  install -m440 -o root -g root deploy/tomcat-cloudflared.sudoers "$SUDOERS"
  if visudo -cf "$SUDOERS" >/dev/null; then
    echo "[sync-units] installed tomcat-cloudflared.sudoers"
  else
    echo "[sync-units] ERROR: new sudoers file is invalid, rolling back" >&2
    if [ -s "$backup" ]; then cp "$backup" "$SUDOERS"; else rm -f "$SUDOERS"; fi
    rm -f "$backup"
    exit 1
  fi
  rm -f "$backup"
fi

if [ ${#CHANGED[@]} -eq 0 ]; then
  echo "[sync-units] units already current"
  exit 0
fi

systemctl daemon-reload
echo "[sync-units] daemon-reload done (${CHANGED[*]})"

# deploy.sh restarts tomcat and cloudflared itself, so only the timer needs
# handling here: nothing else would pick up a changed schedule.
for unit in "${CHANGED[@]}"; do
  if [ "$unit" = "tomcat-deploy.timer" ]; then
    systemctl restart tomcat-deploy.timer
    echo "[sync-units] restarted tomcat-deploy.timer"
  fi
done
