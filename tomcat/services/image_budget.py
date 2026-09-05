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


def _total_ram_bytes() -> int:
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024
    except Exception:
        pass
    return 4 * 1024 ** 3  # assume a small box rather than an unbounded one


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
    return int(_total_ram_bytes() * fraction)


#Seconds a reservation will wait before giving up and proceeding anyway. Blocking
#forever would turn a memory ceiling into a hang; overshooting the budget briefly
#is the lesser failure, and the caller still gets its work done.
_WAIT_TIMEOUT_SEC = max(1.0, float(os.getenv("LABELER_IMAGE_BUDGET_WAIT_SEC", "20") or "20"))


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
            }

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


class _Reservation:
    """Context manager holding a reservation for the life of the decoded image."""

    __slots__ = ("_budget", "_want", "_held")

    def __init__(self, budget: ImageMemoryBudget, nbytes: int) -> None:
        self._budget = budget
        self._want = nbytes
        self._held = 0

    def __enter__(self) -> "_Reservation":
        self._held = self._budget._acquire(self._want)
        return self

    def __exit__(self, *exc) -> None:
        self._budget._release(self._held)
        self._held = 0
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
