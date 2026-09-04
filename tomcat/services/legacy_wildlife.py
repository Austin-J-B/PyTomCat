"""One-time recovery of visually reviewed Melvin and Stove annotations."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from . import local_photos


_MANIFEST_PATH = Path(__file__).resolve().parents[1] / "data" / "legacy_wildlife_annotations.json"
_ALLOWED_LABELS = {"Melvin", "Stove"}


def load_legacy_wildlife_annotations() -> list[dict[str, Any]]:
    """Load and strictly validate the checked-in recovery manifest."""
    payload = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
    raw_annotations = payload.get("annotations")
    if not isinstance(raw_annotations, list):
        raise ValueError("Legacy wildlife manifest has no annotations list")

    annotations: list[dict[str, Any]] = []
    seen_serials: set[int] = set()
    for index, raw in enumerate(raw_annotations, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"Legacy wildlife annotation {index} is not an object")
        serial = int(raw.get("serial") or 0)
        label = str(raw.get("label") or "").strip()
        box = str(raw.get("box") or "").strip()
        if serial <= 0 or serial in seen_serials:
            raise ValueError(f"Legacy wildlife annotation {index} has an invalid serial")
        if label not in _ALLOWED_LABELS:
            raise ValueError(f"Legacy wildlife annotation {index} has an invalid label")
        try:
            values = [float(value) for value in box.split()]
        except ValueError as exc:
            raise ValueError(f"Legacy wildlife annotation {index} has an invalid box") from exc
        if len(values) != 4 or not all(math.isfinite(value) and 0.0 <= value <= 1.0 for value in values):
            raise ValueError(f"Legacy wildlife annotation {index} has an invalid box")
        if values[2] <= 0.0 or values[3] <= 0.0:
            raise ValueError(f"Legacy wildlife annotation {index} has an empty box")
        seen_serials.add(serial)
        annotations.append({"serial": serial, "box_coords": box, "box_cat_ids": label})
    return annotations


def restore_legacy_wildlife_annotations() -> dict[str, Any]:
    """Restore only rows that still have the old blanket Rejected marker."""
    return local_photos.restore_rejected_metadata_annotations(
        load_legacy_wildlife_annotations(),
        actor_name="legacy-wildlife-v1",
    )
