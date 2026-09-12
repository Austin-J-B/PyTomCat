"""A process-wide ceiling on how much decoded image data can be resident at once.

The labeler holds roughly 10MB of decoded pixels per in-flight image operation,
and its concurrency is spread across eleven independent semaphores that each
bound their own path while nothing bounds the sum. Measured with
scripts/labeler_loadgen.py, live images scale linearly with worker count -- 7
workers hold ~42, 16 hold ~100 -- so under enough load the process simply asks
for more memory than the box has and the kernel kills it. Nothing leaks; every
image is released when its work finishes.

So this bounds the total rather than any one path. Reservations are in bytes,
not slots, because the sizes differ by more than an order of magnitude: a
thumbnail crop of a drafted JPEG costs a few MB while a full-resolution decode
costs fifty. Counting slots would either throttle the cheap paths or fail to
bound the expensive ones.

The budget is deliberately generous. It exists to stop the process dying, not to
pace it: a session should reach it rarely, wait briefly, and continue.
"""
from __future__ import annotations

import os
import threading
import time
import weakref
from typing import Any, Optional


#Decoded images are not the only thing resident. torch, the DINOv3 gallery and
#the labeler caches cost well over a gigabyte before a single crop is decoded,
#so the budget has to leave room for them rather than claiming a flat share of
#the machine.
_BASELINE_RESERVE_MB = 1600
_MIN_BUDGET_BYTES = 256 * 1024 * 1024


def _cgroup_limit_bytes() -> Optional[int]:
    """Return the cgroup memory ceiling for this process, if one is set.

    /proc/meminfo reports the host's RAM even when systemd caps the unit with
    MemoryMax, so a budget sized from MemTotal alone would overshoot the cap and
    get the service killed by the very limit meant to contain it. cgroup v2
    first, then v1.
    """
    for path in ("/sys/fs/cgroup/memory.max",
                 "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                raw = fh.read().strip()
        except Exception:
            continue
        if not raw or raw == "max":
            continue
        try:
            value = int(raw)
        except ValueError:
            continue
        #v1 reports a sentinel near 2**63 when the limit is unset.
        if value <= 0 or value >= (1 << 62):
            continue
        return value
    return None


def _total_ram_bytes() -> int:
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024
    except Exception:
        pass
    return 4 * 1024 ** 3  # assume a small box rather than an unbounded one


def _memory_ceiling_bytes() -> int:
    """The real ceiling this process runs under: cgroup limit or host RAM."""
    total = _total_ram_bytes()
    limit = _cgroup_limit_bytes()
    if limit:
        return min(total, limit)
    return total


def _default_budget_bytes() -> int:
    explicit = os.getenv("LABELER_IMAGE_BUDGET_MB")
    if explicit:
        try:
            return max(64, int(explicit)) * 1024 * 1024
        except Exception:
            pass
    fraction = 0.45
    try:
        fraction = float(os.getenv("LABELER_IMAGE_BUDGET_FRACTION", "0.45") or "0.45")
    except Exception:
        pass
    fraction = min(0.9, max(0.05, fraction))

    reserve_mb = _BASELINE_RESERVE_MB
    try:
        reserve_mb = int(float(os.getenv("LABELER_IMAGE_BUDGET_RESERVE_MB", "") or _BASELINE_RESERVE_MB))
    except Exception:
        pass
    reserve = max(0, reserve_mb) * 1024 * 1024

    #Whichever binds first: a share of the ceiling, or what is left after the
    #process's own baseline. On a 4GB box the flat fraction allowed 1.7GB of
    #decoded images on top of a ~1.4GB baseline, which is how it reached 3.4GB
    #and was OOM-killed.
    ceiling = _memory_ceiling_bytes()
    budget = min(int(ceiling * fraction), ceiling - reserve)
    return max(_MIN_BUDGET_BYTES, int(budget))


#Seconds a reservation will wait before giving up and proceeding anyway. Blocking
#forever would turn a memory ceiling into a hang; overshooting the budget briefly
#is the lesser failure, and the caller still gets its work done.
_WAIT_TIMEOUT_SEC = max(1.0, float(os.getenv("LABELER_IMAGE_BUDGET_WAIT_SEC", "20") or "20"))

#---------- What the process actually holds ----------
#
#Everything above accounts for what the decode paths *asked* for. The kills
#happen anyway, so something is holding memory that never went through
#reserve(). Comparing the two is the whole diagnostic: if resident memory
#climbs to the ceiling while in_use_mb stays small, the consumer is outside
#this budget and the numbers here say so instead of looking healthy.

_PAGE_SIZE = getattr(os, "sysconf", lambda _name: 4096)("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
#Report a new high-water mark only once it has moved this far, so a busy
#session logs a climb rather than a line per reservation.
_RSS_LOG_STEP_BYTES = 64 * 1024 * 1024
_RSS_SAMPLE_MIN_INTERVAL_SEC = 0.5


def process_rss_bytes() -> int:
    """Resident memory for this process, or 0 if it cannot be read.

    /proc/self/statm on Linux, which is where this runs: one small read, no
    dependency, cheap enough to sample on every reservation. psutil is the
    fallback for development on other platforms and is not required.
    """
    try:
        with open("/proc/self/statm", "r", encoding="ascii") as handle:
            resident_pages = int(handle.read().split()[1])
        return resident_pages * _PAGE_SIZE
    except Exception:
        pass
    try:
        import psutil
        return int(psutil.Process().memory_info().rss)
    except Exception:
        return 0


class ImageMemoryBudget:
    """Admission control for decoded-image memory."""

    def __init__(self, budget_bytes: Optional[int] = None) -> None:
        self._budget = int(budget_bytes if budget_bytes is not None else _default_budget_bytes())
        self._in_use = 0
        self._lock = threading.Lock()
        self._room = threading.Condition(self._lock)
        self._waits = 0
        self._timeouts = 0
        self._peak = 0
        self._rss_peak = 0
        self._rss_last = 0
        self._rss_sampled_mono = 0.0
        self._rss_reported = 0

    @property
    def budget_bytes(self) -> int:
        return self._budget

    def stats(self) -> dict:
        with self._lock:
            return {
                "budget_mb": int(self._budget / 1048576),
                "in_use_mb": int(self._in_use / 1048576),
                "peak_mb": int(self._peak / 1048576),
                "waits": int(self._waits),
                "timeouts": int(self._timeouts),
                #What the process actually holds, against what was reserved.
                "rss_mb": int(self._rss_last / 1048576),
                "rss_peak_mb": int(self._rss_peak / 1048576),
                #Resident memory this budget cannot account for. This is the
                #number to watch: it is what the OOM killer sees.
                "unaccounted_peak_mb": int(max(0, self._rss_peak - self._peak) / 1048576),
            }

    def _sample_rss_locked(self) -> Optional[tuple]:
        """Record resident memory. Returns a line to log, or None.

        Called with the lock held, from the reservation paths. Throttled,
        because a reservation can happen dozens of times a second and a
        /proc read per decode would show up in the profile.
        """
        now = time.monotonic()
        if (now - self._rss_sampled_mono) < _RSS_SAMPLE_MIN_INTERVAL_SEC:
            return None
        self._rss_sampled_mono = now
        rss = process_rss_bytes()
        if rss <= 0:
            return None
        self._rss_last = rss
        if rss <= self._rss_peak:
            return None
        self._rss_peak = rss
        if (rss - self._rss_reported) < _RSS_LOG_STEP_BYTES:
            return None
        self._rss_reported = rss
        return (
            int(rss / 1048576),
            int(self._in_use / 1048576),
            int(self._peak / 1048576),
            int(self._budget / 1048576),
        )

    def _note_rss(self) -> None:
        """Sample resident memory and log a new high-water mark.

        The log line is the evidence an OOM kill leaves behind: the last one
        written before the process dies says how much was resident and how
        much of it this budget knew about. A large gap means the consumer
        never called reserve() and the ceiling was never going to stop it.

        This depends on the machine log being line-buffered (logger.py opens it
        with buffering=1), so each line is handed to the kernel as it is
        written. A buffered handle would still hold the most interesting lines
        in userspace when SIGKILL arrives, and they would be lost.

        Takes the lock only to sample, and writes the log line outside it:
        holding a decode-admission lock across a file write would serialize
        every reservation in the process behind it.
        """
        with self._lock:
            line = self._sample_rss_locked()
        if line is None:
            return
        rss_mb, in_use_mb, reserved_peak_mb, budget_mb = line
        try:
            from ..logger import log_action
            log_action(
                "image_budget_rss_high_water",
                f"rss={rss_mb}MB; reserved={in_use_mb}MB; reserved_peak={reserved_peak_mb}MB",
                f"budget={budget_mb}MB; unaccounted={max(0, rss_mb - reserved_peak_mb)}MB",
            )
        except Exception:
            pass

    def _acquire(self, nbytes: int) -> int:
        #A single request larger than the whole budget must not deadlock waiting
        #for room that can never exist; let it through alone.
        want = max(0, int(nbytes))
        if want <= 0:
            return 0
        deadline = time.monotonic() + _WAIT_TIMEOUT_SEC
        with self._room:
            if want >= self._budget:
                while self._in_use > 0 and time.monotonic() < deadline:
                    self._waits += 1
                    self._room.wait(timeout=max(0.05, deadline - time.monotonic()))
                self._in_use += want
                self._peak = max(self._peak, self._in_use)
                return want
            while self._in_use + want > self._budget:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._timeouts += 1
                    break
                self._waits += 1
                self._room.wait(timeout=remaining)
            self._in_use += want
            self._peak = max(self._peak, self._in_use)
            return want

    def _release(self, nbytes: int) -> None:
        if nbytes <= 0:
            return
        with self._room:
            self._in_use = max(0, self._in_use - int(nbytes))
            self._room.notify_all()

    def reserve(self, nbytes: int) -> "_Reservation":
        return _Reservation(self, int(nbytes))

    def hold_for(self, obj: Any, nbytes: int, *, wait: bool = True) -> None:
        """Reserve for an object's memory until that object is collected.

        Some paths decode an image and hand the crop back to a caller that keeps
        it across a slow remote call -- tens of seconds on a cold container --
        and that is where the memory actually piles up. A reservation released
        when the producing function returns would miss all of it.

        wait=True gives the ceiling teeth: a worker about to produce another crop
        blocks until earlier ones are collected, which is the only thing that
        bounds a backlog of live crops. Accounting without waiting merely records
        the overshoot after it has already happened.
        """
        want = max(0, int(nbytes))
        if want <= 0:
            return
        if wait:
            self._acquire(want)
        else:
            with self._room:
                self._in_use += want
                self._peak = max(self._peak, self._in_use)
        try:
            weakref.finalize(obj, self._release, want)
        except TypeError:  #object does not support weak references
            self._release(want)
        #hold_for is the path that keeps crops alive across a slow remote call,
        #so it is where resident memory actually piles up.
        self._note_rss()


class _Reservation:
    """Context manager holding a reservation for the life of the decoded image."""

    __slots__ = ("_budget", "_want", "_held")

    def __init__(self, budget: ImageMemoryBudget, nbytes: int) -> None:
        self._budget = budget
        self._want = nbytes
        self._held = 0

    def __enter__(self) -> "_Reservation":
        self._held = self._budget._acquire(self._want)
        #After the reservation, so the sample includes what was just admitted.
        self._budget._note_rss()
        return self

    def __exit__(self, *exc) -> None:
        self._budget._release(self._held)
        self._held = 0
        self._budget._note_rss()
        return None


#One budget for the process; every decode path shares it.
BUDGET = ImageMemoryBudget()


def estimate_decode_bytes(width: int, height: int, *, max_edge: int = 0) -> int:
    """Bytes an RGB decode of this image is expected to hold.

    Counts the decode and the convert("RGB") copy that usually accompanies it.
    When the caller will draft or clamp to max_edge, the estimate follows that
    rather than the file's native size -- otherwise every drafted thumbnail would
    reserve for a full-resolution decode it never performs.
    """
    w = max(1, int(width))
    h = max(1, int(height))
    if max_edge and max(w, h) > max_edge:
        #JPEG draft() only reduces by powers of two and always overshoots upward:
        #asking for 1170 from a 4000px frame yields 2000, not 1170. Estimating the
        #exact target under-reserved by up to 4x and the ceiling never bound.
        steps = 0
        while steps < 3 and max(w, h) // 2 >= max_edge:
            w = max(1, w // 2)
            h = max(1, h // 2)
            steps += 1
    return int(w * h * 3 * 1.5)
