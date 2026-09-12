"""Classify-queue image quality regression: which photos are worth encoding.

Before a labeler photo goes to the classifier it has to clear size and sharpness
gates, and the verdict is cached so the whole queue is not re-decoded. That
verdict used to be assembled in four places; it is now one report builder, one
gate check, and one cached-measurement path.

The measurement is what gets cached, not the pass/fail, because the thresholds
are read from the environment at startup — so a score cached under a looser
setting must not sneak a photo past a stricter one.

Run: python scripts/test_classify_quality.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tomcat.handlers import labeler

FAILURES: List[str] = []


def check(label: str, expected, actual) -> None:
    if expected == actual:
        print(f"  PASS {label}")
    else:
        FAILURES.append(label)
        print(f"  FAIL {label}\n       expected {expected!r}\n       got      {actual!r}")


def gates(pixels: int, min_dim: int, blur: float) -> None:
    labeler._CLASSIFY_MIN_PIXELS = pixels
    labeler._CLASSIFY_MIN_DIM = min_dim
    labeler._CLASSIFY_MIN_BLUR = blur


def clear_caches() -> None:
    labeler._classify_quality_cache.clear()
    labeler._classify_quality_soft_fail_cache.clear()


def main() -> int:
    print("=" * 70)
    print("classify queue image quality regression tests")
    print("=" * 70)

    print("\n[1] the gates name what they reject, in report order")
    gates(122500, 200, 35.0)
    check("a good photo fails nothing", [], labeler._classify_quality_reasons(1000, 1000, 200.0))
    check("too few pixels", ["pixels"], labeler._classify_quality_reasons(300, 300, 200.0))
    #Wide enough overall, but one side is too short to crop from.
    check("too small a side", ["min_dim"], labeler._classify_quality_reasons(2000, 100, 200.0))
    check("too blurry", ["blur"], labeler._classify_quality_reasons(1000, 1000, 5.0))
    check("several at once, in order", ["pixels", "min_dim", "blur"],
          labeler._classify_quality_reasons(100, 100, 1.0))

    print("\n[2] a gate set to zero is switched off")
    gates(0, 0, 0.0)
    check("nothing is rejected", [], labeler._classify_quality_reasons(1, 1, 0.0))

    print("\n[3] the report carries the measurement")
    gates(122500, 0, 35.0)
    report = labeler._classify_quality_report(800, 600, 90.5, [])
    check("dimensions", (800, 600), (report["width"], report["height"]))
    check("pixels derived", 480000, report["pixels"])
    check("blur as a float", 90.5, report["blur"])
    check("passing report is not a hard fail", False, report["hard_fail"])
    report = labeler._classify_quality_report(100, 80, 2.0, ["pixels", "blur"])
    check("a failing report is a hard fail", True, report["hard_fail"])
    check("reasons carried", ["pixels", "blur"], report["reasons"])

    print("\n[4] a fetch or decode failure is soft, not a rejection")
    passes, report = labeler._classify_quality_soft_fail("fetch")
    check("does not pass", False, passes)
    check("reason recorded", ["fetch"], report["reasons"])
    check("but not a hard fail", False, report["hard_fail"])
    check("no measurement claimed", (0, 0, 0, 0.0),
          (report["width"], report["height"], report["pixels"], report["blur"]))

    print("\n[5] a cached measurement is re-gated, not trusted")
    #Cached under gates that let a small photo through...
    gates(0, 0, 0.0)
    clear_caches()
    labeler._cache_set_classify_quality(1, True, 100, 100, 5.0)
    check("passes under the loose gates", True,
          labeler._evaluate_cached_classify_quality(1)[0])
    #...and the same cache entry read under stricter ones.
    gates(122500, 200, 35.0)
    passes, report = labeler._evaluate_cached_classify_quality(1)
    check("fails under the strict gates", False, passes)
    check("and says why", ["pixels", "min_dim", "blur"], report["reasons"])

    print("\n[6] a photo that was never measured has no verdict")
    clear_caches()
    check("no verdict", None, labeler._evaluate_cached_classify_quality(999))
    clear_caches()
    labeler._cache_set_classify_quality_soft_fail(999, "fetch")
    passes, report = labeler._evaluate_cached_classify_quality(999)
    check("a remembered soft failure comes back", (False, ["fetch"], False),
          (passes, report["reasons"], report["hard_fail"]))

    print("\n[7] measuring a photo: fetch, decode, verdict")
    gates(122500, 0, 35.0)

    async def measure(serial, fetch_bytes, metrics):
        clear_caches()

        async def fetch(_serial, _url, **_kwargs):
            return fetch_bytes

        labeler._fetch_image_bytes_for_labeler = fetch
        if metrics is not None:
            labeler._decode_classify_quality_metrics = lambda _data: metrics
        return await labeler._evaluate_classify_quality_uncached(serial, "http://x")

    passes, report = asyncio.run(measure(10, b"", None))
    check("no bytes is a soft fetch failure", (False, ["fetch"], False),
          (passes, report["reasons"], report["hard_fail"]))

    passes, report = asyncio.run(measure(11, b"not-an-image", None))
    check("undecodable bytes are a soft decode failure", (False, ["decode"], False),
          (passes, report["reasons"], report["hard_fail"]))

    passes, report = asyncio.run(measure(12, b"bytes", (800, 600, 90.0)))
    check("a good photo passes", (True, [], 480000),
          (passes, report["reasons"], report["pixels"]))
    check("and its measurement is cached", (True, 800, 600, 90.0),
          labeler._cache_get_classify_quality(12))

    passes, report = asyncio.run(measure(13, b"bytes", (100, 80, 2.0)))
    check("a bad photo is a hard fail", (False, ["pixels", "blur"], True),
          (passes, report["reasons"], report["hard_fail"]))

    print("\n[8] the async entry point agrees with the cached one")
    gates(122500, 0, 35.0)
    clear_caches()
    labeler._cache_set_classify_quality(20, True, 800, 600, 90.0)
    check("cache hit short-circuits",
          labeler._evaluate_cached_classify_quality(20),
          asyncio.run(labeler._evaluate_classify_quality(20, "http://x")))

    print("\n" + "=" * 70)
    if FAILURES:
        print(f"{len(FAILURES)} failure(s).")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
