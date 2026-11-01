"""Regression checks for the deployment privilege boundary.

Run: python scripts/test_deploy_privilege_boundary.py
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _test_support  # noqa: F401, E402

ROOT = Path(__file__).resolve().parents[1]
SUDOERS = (ROOT / "deploy" / "tomcat-cloudflared.sudoers").read_text(encoding="utf-8")
DEPLOY = (ROOT / "deploy" / "deploy.sh").read_text(encoding="utf-8")
HELPER = (ROOT / "deploy" / "tomcat-sync-units.sh").read_text(encoding="utf-8")


def check(label: str, ok: bool) -> None:
    print(f"{'PASS' if ok else 'FAIL'} {label}")
    if not ok:
        raise AssertionError(label)


def main() -> int:
    check("sudoers does not grant the sync helper",
          "/usr/local/sbin/tomcat-sync-units" not in SUDOERS)
    check("deploy.sh does not invoke the sync helper",
          not re.search(r"(?m)^\s*(?:sudo\s+)?/usr/local/sbin/tomcat-sync-units\b", DEPLOY))

    privileged_commands = re.compile(
        r"(?im)^\s*(?:sudo\s+)?(?:install|cp|mv|rm|tee|visudo)\b"
        r"|^\s*(?:sudo\s+)?systemctl\s+daemon-reload\b"
    )
    check("deprecated helper contains no install/copy/mutation/reload commands",
          privileged_commands.search(HELPER) is None)
    check("deprecated helper contains no privileged destination paths",
          not re.search(r"/(?:etc/systemd/system|etc/sudoers(?:\.d)?)(?:/|\b)", HELPER))
    check("deprecated helper fails closed with administrator guidance",
          "exit 1" in HELPER and "administrator" in HELPER.lower())

    # The regular deploy lifecycle must remain intact after removing unit sync.
    check("deploy preserves fast-forward updates and dependency install",
          "git merge --ff-only origin/main" in DEPLOY and '"$VENV_PIP" install -r "$REQ_FILE"' in DEPLOY)
    check("deploy preserves bot and tunnel restarts",
          "sudo systemctl start tomcat" in DEPLOY and "sudo systemctl restart cloudflared" in DEPLOY)
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
