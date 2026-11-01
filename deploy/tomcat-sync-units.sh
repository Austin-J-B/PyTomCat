#!/usr/bin/env bash
# Deprecated: this helper used to install root-owned files from the
# tomcat-writable checkout. It is intentionally fail-closed and must never be
# used to install systemd units or sudoers policy.

set -euo pipefail

echo "[sync-units] ERROR: automatic unit sync is disabled." >&2
echo "[sync-units] Have an administrator review the files and install them from a root-controlled trusted copy." >&2
exit 1
