"""No regression script may write into the real machine log.

logs/machine is the corpus. Production latency, spam accuracy and the OOM
investigation are all analysed from it, and a test record in there is not noise
-- it is wrong data that looks exactly like right data. Before TOMCAT_LOG_DIR
existed, one suite run added 78 lines to it: 63 finance classifications from
test_income_category, spam decisions, and mocked memory readings like
rss=1000MB from the image-budget tests.

Nothing failed when that happened, and nothing would fail if it started again,
which is the whole reason this is a test.

Run: python scripts/test_log_isolation.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "security-and-tests.yml"

FAILURES: List[str] = []


def check(label: str, expected, actual) -> None:
    if expected == actual:
        print(f"  PASS {label}")
    else:
        FAILURES.append(label)
        print(f"  FAIL {label}\n       expected {expected!r}\n       got      {actual!r}")


def ok(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS {label}")
    else:
        FAILURES.append(label)
        print(f"  FAIL {label}{('  -> ' + detail) if detail else ''}")


def main() -> int:
    print("=" * 70)
    print("machine log isolation")
    print("=" * 70)

    print("\n[1] the logger honours TOMCAT_LOG_DIR")
    scratch = tempfile.mkdtemp(prefix="logiso")
    probe = (
        "import sys, pathlib\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "from tomcat.logger import LOG_DIR_MACHINE, log_action\n"
        "log_action('log_isolation_probe', 'trigger', 'output')\n"
        "print(LOG_DIR_MACHINE)\n"
        "print(sum(1 for _ in pathlib.Path(LOG_DIR_MACHINE).rglob('*.ndjson')))\n"
    )
    env = dict(os.environ, TOMCAT_LOG_DIR=scratch)
    proc = subprocess.run([sys.executable, "-c", probe], capture_output=True,
                          text=True, env=env, cwd=str(ROOT))
    out = (proc.stdout or "").strip().splitlines()
    ok("the probe ran", proc.returncode == 0, (proc.stderr or "").strip()[-200:])
    if len(out) >= 2:
        check("the log directory follows the variable", scratch, out[0])
        ok("and a record landed there", int(out[1]) >= 1)

    print("\n[2] _test_support redirects before tomcat.logger is imported")
    #Order matters: the logger reads the directory once, at import, and holds
    #the day's file handle open afterwards.
    probe = (
        "import sys\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        f"sys.path.insert(0, {str(ROOT / 'scripts')!r})\n"
        "import _test_support\n"
        "from tomcat.logger import LOG_DIR_MACHINE\n"
        "print(str(LOG_DIR_MACHINE))\n"
    )
    clean = {k: v for k, v in os.environ.items() if k != "TOMCAT_LOG_DIR"}
    proc = subprocess.run([sys.executable, "-c", probe], capture_output=True,
                          text=True, env=clean, cwd=str(ROOT))
    target = (proc.stdout or "").strip().splitlines()[-1] if proc.stdout.strip() else ""
    ok("it is not the real corpus",
       target and Path(target).resolve() != (ROOT / "logs" / "machine").resolve(),
       f"target={target!r}")

    print("\n[3] every script CI runs imports the redirect")
    #Cheaper and far more legible than running the suite twice and diffing line
    #counts, which is how this was found in the first place.
    try:
        workflow = WORKFLOW.read_text(encoding="utf-8")
    except Exception as exc:
        print(f"  SKIP cannot read {WORKFLOW}: {exc}")
        workflow = ""

    missing: List[str] = []
    checked = 0
    for line in workflow.splitlines():
        line = line.strip()
        if not line.startswith("python scripts/"):
            continue
        name = line.split()[1]
        path = ROOT / name
        if not path.is_file():
            continue
        source = path.read_text(encoding="utf-8")
        #A script that never reaches tomcat cannot log.
        if "tomcat" not in source:
            continue
        #A script may isolate itself another way, but it has to say so in the
        #file, so the exemption is visible to whoever reads it next.
        if "#log-isolation: self-managed" in source:
            continue
        checked += 1
        if "_test_support" not in source:
            missing.append(name)

    ok(f"{checked} scripts checked", checked > 0)
    ok("all of them redirect the log", not missing,
       "missing in: " + ", ".join(missing))
    if missing:
        print("       add this above the first tomcat import:")
        print("         sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))")
        print("         import _test_support  # noqa: F401")

    print("\n" + "=" * 70)
    if FAILURES:
        print(f"{len(FAILURES)} failure(s).")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
