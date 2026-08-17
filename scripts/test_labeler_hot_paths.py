"""Regression tests for the labeler request hot paths.

These cover the three amplification bugs that let one person labeling saturate
the whole web server (which surfaced as Cloudflare 524s on the login endpoint,
since a wedged event loop stalls every request, not just the labeler's):

  1. A ref-crop cache miss forced a full photo-metadata index rebuild, and
     concurrent misses each got their own rebuild instead of sharing one.
  2. Every save cleared every rendered reference crop, so the UI re-rendered
     the whole gallery from disk after each save.
  3. A local photo lookup miss forced a full recursive rescan of the photo
     library, every single time, for serials that are permanently absent.

Run:  python scripts/test_labeler_hot_paths.py
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#The code under test is plain cache bookkeeping, but importing the labeler
#module drags in the CV and Google stacks. Stub whatever is not installed so
#this runs on a bare checkout like the other scripts/test_*.py do.
_OPTIONAL_DEPS = (
    "torch", "torch.nn", "torch.nn.functional", "torchvision",
    "torchvision.transforms", "ultralytics", "cv2", "numpy", "discord",
    "gspread", "gspread.auth", "gspread.exceptions", "gspread.utils",
    "google", "google.oauth2", "google.oauth2.service_account", "modal",
)
for _dep in _OPTIONAL_DEPS:
    try:
        __import__(_dep)
    except Exception:
        from unittest.mock import MagicMock

        sys.modules[_dep] = MagicMock()

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print("  %-4s %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -> " + detail))
    if not ok:
        FAILURES.append(name)


def test_crop_index_rebuild_is_shared() -> None:
    """Concurrent forced rebuilds collapse into one full metadata scan."""
    from tomcat.handlers import labeler

    builds = {"n": 0}

    def fake_build():
        builds["n"] += 1
        return {(1, 1): {"serial": 1, "crop": 1, "url": "", "box": "0.5 0.5 0.2 0.2"}}

    original_build = labeler._build_photo_crop_index_cache
    labeler._build_photo_crop_index_cache = fake_build
    labeler._photo_crop_index_cache = {}
    labeler._photo_crop_index_built_mono = 0.0
    try:
        async def run() -> None:
            #First call populates the (empty) cache.
            await labeler._ensure_photo_crop_index_cache(force=False)
            #Then twelve simultaneous misses, matching the UI's ref-image
            #concurrency, all asking for a forced rebuild at once.
            await asyncio.gather(*[
                labeler._ensure_photo_crop_index_cache(force=True) for _ in range(12)
            ])

        asyncio.run(run())
        check(
            "12 concurrent forced rebuilds cost at most 2 metadata scans",
            builds["n"] <= 2,
            "scans=%d" % builds["n"],
        )

        #A crop that is absent from the metadata misses on every request, so the
        #miss path has to stop rebuilding after the first attempt.
        builds["n"] = 0
        labeler._photo_crop_index_miss_rebuild_next_mono = 0.0

        async def run_misses() -> None:
            for _ in range(20):
                await labeler._refresh_photo_crop_index_after_miss()

        asyncio.run(run_misses())
        check(
            "20 sequential misses cause at most 1 metadata scan",
            builds["n"] <= 1,
            "scans=%d" % builds["n"],
        )
    finally:
        labeler._build_photo_crop_index_cache = original_build
        labeler._photo_crop_index_cache = {}
        labeler._photo_crop_index_built_mono = 0.0
        labeler._photo_crop_index_miss_rebuild_next_mono = 0.0


def test_save_evicts_only_touched_serials() -> None:
    """A save drops its own rendered crops and leaves everyone else's cached."""
    from tomcat.handlers import labeler

    labeler._ref_crop_result_cache.clear()
    labeler._ref_crop_cache_keys_by_serial.clear()

    keys = {}
    for serial in (11, 12, 13):
        key = labeler._ref_crop_cache_key(serial, 1, 128)
        keys[serial] = key
        labeler._cache_set_bytes(
            labeler._ref_crop_result_cache,
            key,
            b"jpeg-bytes",
            max_items=labeler._REF_CROP_RESULT_CACHE_MAX,
            ttl_sec=labeler._REF_CROP_RESULT_TTL_SEC,
        )
        labeler._remember_ref_crop_cache_key(serial, key)

    dropped = labeler._drop_ref_crop_renders_for_serials([12])
    check("saving serial 12 evicts exactly its own render", dropped == 1, "dropped=%d" % dropped)
    check("evicted serial is gone", keys[12] not in labeler._ref_crop_result_cache)
    check(
        "untouched serials stay cached",
        keys[11] in labeler._ref_crop_result_cache and keys[13] in labeler._ref_crop_result_cache,
    )

    labeler._ref_crop_result_cache.clear()
    labeler._ref_crop_cache_keys_by_serial.clear()


def test_unresolvable_author_is_not_refetched() -> None:
    """An author Discord cannot resolve goes into backoff instead of re-fetching."""
    from tomcat.handlers import labeler

    labeler._discord_context_unresolved.clear()
    key = ("author", 555, 777)
    check("unknown author starts resolvable", not labeler._context_lookup_failed(key))
    labeler._mark_context_lookup_failed(key)
    check("failed author is skipped on the next pass", labeler._context_lookup_failed(key))
    labeler._discord_context_unresolved.clear()


def test_missing_serial_does_not_rescan_every_time() -> None:
    """Repeated lookups of an absent serial trigger at most one library rescan."""
    from tomcat.services import local_photos

    root = Path(tempfile.mkdtemp(prefix="tomcat_photos_"))
    scans = {"n": 0}
    original_scan = local_photos._scan_index
    original_root = local_photos.photo_root

    def counting_scan(scan_root, exts):
        scans["n"] += 1
        return original_scan(scan_root, exts)

    try:
        (root / "sn0001.jpg").write_bytes(b"not-a-real-jpeg")
        local_photos.photo_root = lambda: root
        local_photos._scan_index = counting_scan
        local_photos._INDEX_NEXT_REFRESH_MONO = 0.0
        local_photos._INDEX_ROOT_SIG = ("", 0, 0)
        local_photos._INDEX_MISS_RESCAN_NEXT_MONO = 0.0

        found = local_photos.get_local_photo_path(1)
        check("present serial resolves", found is not None)

        scans["n"] = 0
        for _ in range(25):
            #Serial 9999 has no file and never will, which is the case that used
            #to walk the whole library on each call.
            local_photos.get_local_photo_path(9999)
        check(
            "25 lookups of an absent serial cause at most 1 rescan",
            scans["n"] <= 1,
            "scans=%d" % scans["n"],
        )
    finally:
        local_photos._scan_index = original_scan
        local_photos.photo_root = original_root
        local_photos._INDEX_NEXT_REFRESH_MONO = 0.0
        local_photos._INDEX_ROOT_SIG = ("", 0, 0)
        local_photos._INDEX_MISS_RESCAN_NEXT_MONO = 0.0
        shutil.rmtree(root, ignore_errors=True)


def main() -> int:
    print("labeler hot paths")
    print("=" * 70)
    print("crop index rebuild")
    test_crop_index_rebuild_is_shared()
    print("ref crop eviction on save")
    test_save_evicts_only_touched_serials()
    print("discord context backoff")
    test_unresolvable_author_is_not_refetched()
    print("local photo lookup")
    test_missing_serial_does_not_rescan_every_time()

    print("\n" + "=" * 70)
    if FAILURES:
        print("FAILED (%d): %s" % (len(FAILURES), ", ".join(FAILURES)))
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
