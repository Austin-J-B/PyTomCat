"""Regression test for the decoded-image memory ceiling.

The budget used to take a flat 45% of MemTotal. On the 4GB production box that
allowed ~1.7GB of decoded images on top of the ~1.4GB the process already needs
for torch, the gallery and the labeler caches, so it climbed to ~3.4GB and the
kernel OOM-killed it -- repeatedly, taking sshd and the tunnel with it.

Three things have to hold now:
  * the ceiling follows the cgroup limit, because /proc/meminfo reports host RAM
    even when systemd caps the unit with MemoryMax;
  * the budget leaves the process's own baseline out of its share;
  * resident memory is recorded next to what was reserved. The kills continued
    after the ceiling shipped, so something holds memory that never calls
    reserve(); the gap between the two is what identifies it, and it has to
    reach the log before the kernel kills the process.

Run:  python scripts/test_image_budget.py
"""
from __future__ import annotations

import io
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tomcat.services import image_budget as IB  # noqa: E402

MB = 1024 * 1024
GB = 1024 ** 3
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print("  %-4s %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -> " + detail))
    if not ok:
        FAILURES.append(name)


def budget_with(*, ceiling: int, env: dict | None = None) -> int:
    clean = {k: v for k, v in os.environ.items()
             if not k.startswith("LABELER_IMAGE_BUDGET")}
    clean.update(env or {})
    with mock.patch.dict(os.environ, clean, clear=True), \
            mock.patch.object(IB, "_memory_ceiling_bytes", lambda: ceiling):
        return IB._default_budget_bytes()


def test_cgroup_limit_parsing():
    print("\n[1] cgroup limit parsing")

    def reader(content: str):
        def _open(path, *a, **kw):
            if path == "/sys/fs/cgroup/memory.max":
                return io.StringIO(content)
            raise FileNotFoundError(path)
        return _open

    with mock.patch("builtins.open", reader("2936012800")):
        check("v2 numeric limit is used", IB._cgroup_limit_bytes() == 2936012800,
              str(IB._cgroup_limit_bytes()))
    with mock.patch("builtins.open", reader("max")):
        check("'max' means unlimited", IB._cgroup_limit_bytes() is None,
              str(IB._cgroup_limit_bytes()))
    with mock.patch("builtins.open", reader(str(1 << 63))):
        check("v1 unlimited sentinel ignored", IB._cgroup_limit_bytes() is None,
              str(IB._cgroup_limit_bytes()))
    with mock.patch("builtins.open", reader("not-a-number")):
        check("garbage ignored", IB._cgroup_limit_bytes() is None,
              str(IB._cgroup_limit_bytes()))


def test_ceiling_prefers_cgroup():
    print("\n[2] ceiling follows the cgroup, not the host")
    with mock.patch.object(IB, "_total_ram_bytes", lambda: 4 * GB), \
            mock.patch.object(IB, "_cgroup_limit_bytes", lambda: 2800 * MB):
        check("capped unit uses its cgroup limit",
              IB._memory_ceiling_bytes() == 2800 * MB, str(IB._memory_ceiling_bytes()))
    with mock.patch.object(IB, "_total_ram_bytes", lambda: 4 * GB), \
            mock.patch.object(IB, "_cgroup_limit_bytes", lambda: None):
        check("uncapped unit uses host RAM",
              IB._memory_ceiling_bytes() == 4 * GB, str(IB._memory_ceiling_bytes()))


def test_budget_leaves_baseline_room():
    print("\n[3] budget leaves room for the process baseline")
    capped = budget_with(ceiling=2800 * MB)
    check("under MemoryMax=2.8G the budget is headroom-bound",
          capped == 2800 * MB - 1600 * MB, "%dMB" % (capped / MB))
    check("and peak stays under the cap",
          capped + 1600 * MB <= 2800 * MB, "%dMB" % (capped / MB))

    roomy = budget_with(ceiling=16 * GB)
    check("on a large box the fraction still binds",
          roomy == int(16 * GB * 0.45), "%dMB" % (roomy / MB))

    tiny = budget_with(ceiling=1 * GB)
    check("a ceiling below the reserve falls back to the floor",
          tiny == IB._MIN_BUDGET_BYTES, "%dMB" % (tiny / MB))


def test_env_overrides():
    print("\n[4] explicit overrides still win")
    check("LABELER_IMAGE_BUDGET_MB is absolute",
          budget_with(ceiling=2800 * MB, env={"LABELER_IMAGE_BUDGET_MB": "700"}) == 700 * MB)
    #A bigger reserve has to bind before the fraction does, or it proves nothing:
    #at 800MB the 45% share is the smaller of the two and wins on its own.
    check("a larger reserve shrinks the budget",
          budget_with(ceiling=2800 * MB, env={"LABELER_IMAGE_BUDGET_RESERVE_MB": "2400"})
          == 400 * MB,
          "%dMB" % (budget_with(ceiling=2800 * MB,
                                env={"LABELER_IMAGE_BUDGET_RESERVE_MB": "2400"}) / MB))
    check("fraction is still honoured",
          budget_with(ceiling=2800 * MB, env={"LABELER_IMAGE_BUDGET_FRACTION": "0.10"})
          == int(2800 * MB * 0.10))


def test_rss_tracking():
    """Resident memory is recorded against what was reserved.

    The ceiling bounds what the decode paths ask for, and the box still gets
    OOM-killed -- so something holds memory that never calls reserve(). These
    numbers are the evidence: a large unaccounted figure says the consumer is
    outside this budget and no ceiling here would have stopped it.
    """
    print("\n[5] resident memory tracking")

    budget = IB.ImageMemoryBudget(budget_bytes=512 * MB)
    fake_rss = {"value": 300 * MB}

    with mock.patch.object(IB, "process_rss_bytes", lambda: fake_rss["value"]):
        with budget.reserve(64 * MB):
            pass
        stats = budget.stats()
    check("resident memory is reported", stats["rss_mb"] == 300,
          "rss_mb=%r" % stats["rss_mb"])
    check("and its high-water mark", stats["rss_peak_mb"] == 300,
          "rss_peak_mb=%r" % stats["rss_peak_mb"])
    #300MB resident, 64MB of it reserved: the rest is the interesting part.
    check("memory the budget cannot account for",
          stats["unaccounted_peak_mb"] == 300 - 64,
          "unaccounted=%r" % stats["unaccounted_peak_mb"])

    print("\n[6] the high-water mark only moves up, and is throttled")
    budget = IB.ImageMemoryBudget(budget_bytes=512 * MB)
    fake_rss = {"value": 1000 * MB}
    with mock.patch.object(IB, "process_rss_bytes", lambda: fake_rss["value"]):
        with budget.reserve(MB):
            pass
        peak_high = budget.stats()["rss_peak_mb"]
        #A later, smaller sample must not lower the peak: the peak is what the
        #OOM killer reacted to.
        fake_rss["value"] = 100 * MB
        budget._rss_sampled_mono = 0.0
        with budget.reserve(MB):
            pass
        after = budget.stats()
    check("the peak holds", peak_high == 1000 and after["rss_peak_mb"] == 1000,
          "peak=%r then %r" % (peak_high, after["rss_peak_mb"]))
    check("the current reading follows the process down",
          after["rss_mb"] == 100, "rss_mb=%r" % after["rss_mb"])

    #Sampling is throttled, or a busy session would read /proc per decode.
    budget = IB.ImageMemoryBudget(budget_bytes=512 * MB)
    reads = {"n": 0}

    def counting_rss():
        reads["n"] += 1
        return 200 * MB

    with mock.patch.object(IB, "process_rss_bytes", counting_rss):
        for _ in range(50):
            with budget.reserve(MB):
                pass
    check("50 reservations do not mean 100 /proc reads", reads["n"] <= 2,
          "reads=%d" % reads["n"])

    print("\n[7] an unreadable /proc is not fatal")
    budget = IB.ImageMemoryBudget(budget_bytes=512 * MB)
    with mock.patch.object(IB, "process_rss_bytes", lambda: 0):
        with budget.reserve(MB):
            pass
        stats = budget.stats()
    check("the budget still works", stats["rss_mb"] == 0 and stats["budget_mb"] == 512,
          "stats=%r" % stats)


def test_rss_reader():
    print("\n[8] the reader itself")
    value = IB.process_rss_bytes()
    #0 on a platform without /proc and without psutil; a real size otherwise.
    check("returns a plausible value", value == 0 or value > 1024 * 1024,
          "value=%r" % value)

    #The production host is Linux, so /proc/self/statm is the path that
    #actually runs -- development is usually somewhere that falls through to
    #psutil, which would leave the real parse untested.
    #Fields: size resident shared text lib data dt, in pages.
    real_open = open

    def statm_open(path, *a, **kw):
        if path == "/proc/self/statm":
            return io.StringIO("132000 45678 3210 12 0 90000 0\n")
        return real_open(path, *a, **kw)

    with mock.patch("builtins.open", statm_open):
        parsed = IB.process_rss_bytes()
    check("the second field is resident pages",
          parsed == 45678 * IB._PAGE_SIZE,
          "parsed=%r expected=%r" % (parsed, 45678 * IB._PAGE_SIZE))

    def broken_open(path, *a, **kw):
        if path == "/proc/self/statm":
            return io.StringIO("garbage\n")
        return real_open(path, *a, **kw)

    with mock.patch("builtins.open", broken_open):
        with mock.patch.dict(sys.modules, {"psutil": None}):
            check("garbage reads as unknown, not a crash",
                  IB.process_rss_bytes() == 0)


def main():
    print("=" * 70)
    print("image budget regression tests")
    print("=" * 70)
    test_cgroup_limit_parsing()
    test_ceiling_prefers_cgroup()
    test_budget_leaves_baseline_room()
    test_env_overrides()
    test_rss_tracking()
    test_rss_reader()
    print("\n" + "=" * 70)
    if FAILURES:
        print("FAILED (%d): %s" % (len(FAILURES), ", ".join(FAILURES)))
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
