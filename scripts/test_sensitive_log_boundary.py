"""Regression checks for source policy around sensitive operational logs.

Run: python scripts/test_sensitive_log_boundary.py

This checks the ignore policy and only rejects present files that are not
ignored by that policy. Production runtime logs may remain locally; the test
does not inspect or print their contents. For checked-in source, it compares
the Git index with pending worktree deletions so it passes before staging and
after staging without mistaking ignored local runtime files for source data.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GITIGNORE = ROOT / ".gitignore"
SENSITIVE_DIRS = (ROOT / "logs" / "subs", ROOT / "logs" / "moderation")
EXPECTED_RULES = {"logs/subs/", "logs/moderation/"}


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'} {label}{(': ' + detail) if detail and not ok else ''}")
    if not ok:
        raise AssertionError(label)


def is_ignored(path: Path) -> bool:
    relative = path.relative_to(ROOT).as_posix()
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "--quiet", "--", relative],
        cwd=ROOT,
        check=False,
    )
    return result.returncode == 0


def git_paths(*args: str) -> set[str]:
    result = subprocess.run(
        ["git", *args, "-z", "--", "logs/subs", "logs/moderation"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return {
        item.decode("utf-8", errors="surrogateescape").replace("\\", "/")
        for item in result.stdout.split(b"\0")
        if item
    }


def main() -> int:
    lines = {
        line.strip()
        for line in GITIGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    check("exact sensitive-category ignore rules are active",
          EXPECTED_RULES <= lines)
    check("unrelated log categories are not broadly ignored",
          "logs/*" not in lines)

    tracked = git_paths("ls-files")
    pending_deletions = git_paths("diff", "--name-only", "--diff-filter=D")
    remaining_tracked = tracked - pending_deletions
    check("no sensitive log paths remain in the tracked source representation",
          not remaining_tracked, ", ".join(sorted(remaining_tracked)))

    # Existing ignored runtime files are allowed, while any unignored file
    # under these categories fails. This does not rely on index membership.
    unexpected = []
    for directory in SENSITIVE_DIRS:
        if not directory.exists():
            continue
        for current, _dirs, names in os.walk(directory):
            for name in names:
                path = Path(current) / name
                if not is_ignored(path):
                    unexpected.append(path.relative_to(ROOT).as_posix())
    check("no non-ignored sensitive log files are present",
          not unexpected, ", ".join(unexpected))
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
