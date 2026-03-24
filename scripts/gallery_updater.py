#!/usr/bin/env python3
"""Manual entrypoint for rebuilding the active DINOv3 gallery."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tomcat.services.gallery_updater import (
    run_gallery_update,
    _DEFAULT_DOWNLOAD_WORKERS,
    _DEFAULT_DOWNLOAD_CHUNK_SIZE,
    _DEFAULT_PROGRESS_LOG_SEC,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Rebuild gallery from labeled crops in local photo metadata.")
    parser.add_argument(
        "--mode",
        default="full",
        choices=["full", "incremental"],
        # "incremental" is accepted but currently coerced to "full" inside the service
        # for correctness — label corrections must always be reflected.
        help="Gallery update mode. 'incremental' currently resolves to 'full' for correctness. (default: full)",
    )
    parser.add_argument(
        "--gallery-version",
        default=None,
        # Examples: "5", "5.1", "5.1.2"  →  produces R5_cat_DINOv3_gallery.pt, etc.
        # Omit to auto-increment from the highest existing versioned file in weights/.
        help=(
            "Explicit version string for the output file, e.g. '5' → R5_cat_DINOv3_gallery.pt, "
            "'5.1.2' → R5.1.2_cat_DINOv3_gallery.pt. "
            "Omit to auto-increment from the highest existing version in weights/."
        ),
    )
    hflip_group = parser.add_mutually_exclusive_group()
    hflip_group.add_argument(
        "--tta-hflip",
        dest="tta_hflip",
        action="store_true",
        # Each crop is embedded twice (original + mirrored) and the embeddings are averaged.
        # Improves robustness for cats that are often photographed from one side, at the cost
        # of roughly 2× embedding time.
        help="Enable horizontal-flip TTA: embed each crop twice (normal + mirrored) and average. Slower but more robust.",
    )
    hflip_group.add_argument(
        "--no-tta-hflip",
        dest="tta_hflip",
        action="store_false",
        help="Disable horizontal-flip TTA (embed each crop once). Faster.",
    )
    parser.set_defaults(tta_hflip=None)  # None → inherit from GALLERY_TTA_HFLIP env var
    parser.add_argument(
        "--disable-local-photos",
        action="store_true",
        # When enabled, only the Google Sheet metadata is used (no cache/PicsOfCats images).
        # Useful for a quick test run or when the local photo store is unavailable.
        help="Skip the local supervised photo store (cache/PicsOfCats). Use only sheet-linked images.",
    )
    parser.add_argument(
        "--image-workers",
        type=int,
        default=None,
        # Controls how many threads fetch/decode images in parallel during the crop-build phase.
        # The service auto-computes max(4, min(24, cpu_count × 0.75)) when unset; override here
        # if you want to throttle (e.g. --image-workers 4 on a shared machine) or push harder.
        # Env var: GALLERY_DOWNLOAD_WORKERS
        help=(
            f"Parallel worker threads for image fetch/decode (default: auto = {_DEFAULT_DOWNLOAD_WORKERS} on this machine). "
            "Lower values reduce CPU/memory pressure; higher values speed up large retrains. "
            "Override env GALLERY_DOWNLOAD_WORKERS."
        ),
    )
    parser.add_argument(
        "--image-chunk-size",
        type=int,
        default=None,
        # Number of sheet rows loaded into memory at once before submitting to the thread pool.
        # The service default is max(128, min(1024, workers × 32)).
        # Increase for faster throughput on large datasets; decrease if memory is tight.
        # Env var: GALLERY_DOWNLOAD_CHUNK_SIZE
        help=(
            f"Sheet rows per image-fetch chunk (default: auto = {_DEFAULT_DOWNLOAD_CHUNK_SIZE} on this machine). "
            "Larger chunks improve throughput; smaller chunks reduce peak memory. "
            "Override env GALLERY_DOWNLOAD_CHUNK_SIZE."
        ),
    )
    parser.add_argument(
        "--progress-log-sec",
        type=float,
        default=None,
        # How often (in seconds) a progress line is written to the action log during embedding.
        # Lower values give more granular output; minimum enforced by the service is 5 s.
        # Env var: GALLERY_PROGRESS_LOG_SEC
        help=(
            f"Seconds between progress log lines during embedding (default: {_DEFAULT_PROGRESS_LOG_SEC:.0f}s, min 5). "
            "Override env GALLERY_PROGRESS_LOG_SEC."
        ),
    )
    args = parser.parse_args()

    kwargs = {
        "mode": args.mode,
        "gallery_version": args.gallery_version,
        "use_local_photos": not args.disable_local_photos,
    }
    if args.tta_hflip is not None:
        kwargs["tta_hflip"] = bool(args.tta_hflip)
    if args.image_workers is not None:
        kwargs["download_workers"] = int(args.image_workers)
    if args.image_chunk_size is not None:
        kwargs["download_chunk_size"] = int(args.image_chunk_size)
    if args.progress_log_sec is not None:
        kwargs["progress_log_sec"] = float(args.progress_log_sec)

    result = run_gallery_update(**kwargs)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if str(result.get("status")) == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
