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
    parser = argparse.ArgumentParser(description="Rebuild gallery from labeled crops in TCB Pics Formatted.")
    parser.add_argument(
        "--mode",
        default="full",
        choices=["full", "incremental"],
        help="Gallery update mode. Incremental currently resolves to full for correctness.",
    )
    parser.add_argument(
        "--no-tta-hflip",
        action="store_true",
        help="Disable horizontal-flip TTA while embedding crops.",
    )
    args = parser.parse_args()

    result = run_gallery_update(mode=args.mode, tta_hflip=not args.no_tta_hflip)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if str(result.get("status")) == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
