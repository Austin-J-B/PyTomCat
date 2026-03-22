#!/usr/bin/env python3
"""Run the CatDatabase photo-derived column sync once."""

from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tomcat.services.catabase_sheet_sync import sync_catabase_photo_columns


def main() -> int:
    result = sync_catabase_photo_columns()
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("status") in {"ok", "skipped"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
