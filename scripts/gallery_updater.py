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

from tomcat.services.gallery_updater import run_gallery_update


def main() -> int:
    parser = argparse.ArgumentParser(description="Rebuild gallery from labeled crops in local photo metadata.")
    parser.add_argument(
        "--mode",
        default="full",
        choices=["full", "incremental"],
        help="Gallery update mode. Incremental currently resolves to full for correctness.",
    )
    parser.add_argument(
        "--gallery-version",
        default=None,
        help="Write an explicit gallery version like 5.0.0, producing R5.0.0_cat_DINOv3_gallery.pt.",
    )
    hflip_group = parser.add_mutually_exclusive_group()
    hflip_group.add_argument(
        "--tta-hflip",
        dest="tta_hflip",
        action="store_true",
        help="Enable horizontal-flip TTA while embedding crops.",
    )
    hflip_group.add_argument(
        "--no-tta-hflip",
        dest="tta_hflip",
        action="store_false",
        help="Disable horizontal-flip TTA while embedding crops.",
    )
    parser.set_defaults(tta_hflip=None)
    parser.add_argument(
        "--disable-local-photos",
        action="store_true",
        help="Do not read from local supervised photo store (cache/PicsOfCats).",
    )
    parser.add_argument(
        "--image-workers",
        type=int,
        default=None,
        help="Parallel workers for image fetch/decode pipeline (default from env).",
    )
    parser.add_argument(
        "--image-chunk-size",
        type=int,
        default=None,
        help="Rows per image-fetch chunk before crop extraction (default from env).",
    )
    parser.add_argument(
        "--progress-log-sec",
        type=float,
        default=None,
        help="Progress log cadence in seconds (default from env).",
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
