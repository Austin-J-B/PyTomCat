#!/usr/bin/env python3
"""Legacy utility for importing existing detector and classifier labels into TCB Pics Formatted.

Reads:
  - LabelingApps/PreviousDetectorLabels/labels/snXXXX.txt → BoxCoordinates (col I)
  - LabelingApps/PreviousClassifierLabels/known_cats/{CatName}/snXXXX.jpg → BoxCatIDs (col J)
  - LabelingApps/PreviousClassifierLabels/HITL_labeled_crops/{CatName}/snXXXX_cropN.jpg → BoxCatIDs

Writes:
  - BoxCoordinates: pipe-separated YOLO boxes "cx cy w h|cx cy w h|..."
  - BoxCatIDs: pipe-separated cat names matching box order "Twix|Hershey|..."
  - "Rejected" in BoxCoordinates for images omitted from detector labels

Usage:
    python scripts/import_labels.py --dry-run
    python scripts/import_labels.py --commit
"""
from __future__ import annotations
import argparse
import os
import re
import sys
from pathlib import Path
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

DETECTOR_LABELS_DIR = ROOT / "LabelingApps" / "PreviousDetectorLabels" / "labels"
KNOWN_CATS_DIR = ROOT / "LabelingApps" / "PreviousClassifierLabels" / "known_cats"
HITL_CROPS_DIR = ROOT / "LabelingApps" / "PreviousClassifierLabels" / "HITL_labeled_crops"

#Columns in TCB Pics Formatted sheet (0-indexed)
#Actual layout: A=CatID, B=IDHelper, C=Date, D=Time, E=Person, F=Spacer, G=URL, H=Serial
COL_CAT_ID = 0           #A: Cat ID (e.g., "1. Twix")
COL_URL = 6              #G: Picture Link
COL_SERIAL = 7           #H: Serial number
COL_BOX_COORDS = 8       #I: BoxCoordinates (new)
COL_BOX_CAT_IDS = 9      #J: BoxCatIDs (new)

#Regex to extract serial number from filenames
SN_PATTERN = re.compile(r"sn(\d+)")
CROP_PATTERN = re.compile(r"sn(\d+)_crop(\d+)")


def parse_serial(filename: str) -> Optional[int]:
    """Extract serial number from filename like 'sn1234.txt' or 'sn1234_crop0.jpg'."""
    m = SN_PATTERN.search(filename)
    return int(m.group(1)) if m else None


def parse_crop_index(filename: str) -> Tuple[Optional[int], Optional[int]]:
    """Extract (serial, crop_index) from 'snXXXX_cropN.jpg'."""
    m = CROP_PATTERN.search(filename)
    if m:
        return int(m.group(1)), int(m.group(2))
    #Single-cat image in known_cats: snXXXX.jpg -> crop 0
    m = SN_PATTERN.search(filename)
    if m:
        return int(m.group(1)), 0
    return None, None


def load_detector_labels() -> Dict[int, List[str]]:
    """Load detector labels: serial -> list of 'cx cy w h' strings."""
    labels: Dict[int, List[str]] = {}
    if not DETECTOR_LABELS_DIR.exists():
        print(f"[warn] Detector labels dir not found: {DETECTOR_LABELS_DIR}")
        return labels
    
    for txt_file in DETECTOR_LABELS_DIR.glob("sn*.txt"):
        sn = parse_serial(txt_file.name)
        if sn is None:
            continue
        boxes = []
        for line in txt_file.read_text(encoding="utf-8").strip().splitlines():
            parts = line.strip().split()
            if len(parts) >= 5:
                #YOLO format: class cx cy w h -> extract cx cy w h
                cx, cy, w, h = parts[1:5]
                boxes.append(f"{cx} {cy} {w} {h}")
        if boxes:
            labels[sn] = boxes
    
    print(f"[info] Loaded {len(labels)} detector labels")
    return labels


def load_classifier_labels() -> Dict[int, Dict[int, str]]:
    """Load classifier labels: serial -> {crop_index: cat_name}."""
    labels: Dict[int, Dict[int, str]] = defaultdict(dict)
    
    #1. known_cats: single-cat images (crop 0)
    if KNOWN_CATS_DIR.exists():
        for cat_dir in KNOWN_CATS_DIR.iterdir():
            if not cat_dir.is_dir():
                continue
            cat_name = cat_dir.name
            for img_file in cat_dir.glob("sn*.jpg"):
                sn, crop_idx = parse_crop_index(img_file.name)
                if sn is not None and crop_idx is not None:
                    labels[sn][crop_idx] = cat_name
    
    #2. HITL_labeled_crops: multi-cat crops
    if HITL_CROPS_DIR.exists():
        for cat_dir in HITL_CROPS_DIR.iterdir():
            if not cat_dir.is_dir():
                continue
            cat_name = cat_dir.name
            for img_file in cat_dir.glob("sn*_crop*.jpg"):
                sn, crop_idx = parse_crop_index(img_file.name)
                if sn is not None and crop_idx is not None:
                    labels[sn][crop_idx] = cat_name
    
    print(f"[info] Loaded classifier labels for {len(labels)} serials")
    return dict(labels)


def find_cutoff_serial(detector_labels: Dict[int, List[str]]) -> int:
    """Find highest serial in detector labels (cutoff for retroactive reject)."""
    if not detector_labels:
        return 0
    return max(detector_labels.keys())


def build_import_data(
    sheet_rows: List[List[str]],
    detector_labels: Dict[int, List[str]],
    classifier_labels: Dict[int, Dict[int, str]],
    cutoff_serial: int
) -> List[Tuple[int, str, str]]:
    """Build list of (row_index, box_coords, box_cat_ids) for import.
    
    Returns row indices (0-indexed from data, not header) and values to write.
    """
    updates = []
    
    for row_idx, row in enumerate(sheet_rows):
        if len(row) < 1:
            continue
        
        #Parse serial from column H (could be plain number or snXXXX)
        serial_str = row[COL_SERIAL] if len(row) > COL_SERIAL else ""
        sn_match = SN_PATTERN.search(serial_str)
        if sn_match:
            sn = int(sn_match.group(1))
        elif serial_str.strip().isdigit():
            sn = int(serial_str.strip())
        else:
            continue
        
        #Existing cat ID in column B (for NotACat check)
        existing_cat_id = row[COL_CAT_ID] if len(row) > COL_CAT_ID else ""
        is_not_a_cat = existing_cat_id.strip().lower().startswith("0. notacat")
        
        #Check if already has BoxCoordinates
        existing_coords = row[COL_BOX_COORDS] if len(row) > COL_BOX_COORDS else ""
        if existing_coords.strip():
            continue  #Skip already-labeled rows
        
        #Build BoxCoordinates
        if sn in detector_labels:
            boxes = detector_labels[sn]
            box_coords = "|".join(boxes)
        elif sn <= cutoff_serial:
            #Missing from detector labels but within cutoff -> Rejected
            box_coords = "Rejected"
        else:
            #New image beyond cutoff -> leave empty for future labeling
            box_coords = ""
        
        #Build BoxCatIDs
        if sn in classifier_labels:
            crop_map = classifier_labels[sn]
            num_boxes = len(detector_labels.get(sn, []))
            cat_ids = []
            for i in range(num_boxes):
                cat_ids.append(crop_map.get(i, ""))
            box_cat_ids = "|".join(cat_ids)
        elif is_not_a_cat and box_coords != "Rejected":
            #Only NotACat in column B -> Reject
            box_coords = "Rejected"
            box_cat_ids = ""
        else:
            box_cat_ids = ""
        
        #Only add if we have something to write
        if box_coords or box_cat_ids:
            updates.append((row_idx, box_coords, box_cat_ids))
    
    return updates


def get_sheet_client():
    """Get authenticated gspread client."""
    import gspread
    from tomcat.config import settings
    
    creds_path = settings.google_service_account_json
    gc = gspread.service_account(filename=creds_path)
    return gc


def main() -> None:
    parser = argparse.ArgumentParser(description="Import detector/classifier labels to sheet")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without writing")
    parser.add_argument("--commit", action="store_true", help="Actually write to sheet")
    parser.add_argument("--limit", type=int, default=0, help="Limit rows to process (0=all)")
    args = parser.parse_args()
    
    if not args.dry_run and not args.commit:
        parser.error("Must specify --dry-run or --commit")
    
    #Load labels
    detector_labels = load_detector_labels()
    classifier_labels = load_classifier_labels()
    cutoff_serial = find_cutoff_serial(detector_labels)
    print(f"[info] Cutoff serial (highest in detector labels): sn{cutoff_serial:04d}")
    
    #Load sheet data
    from tomcat.config import settings
    gc = get_sheet_client()
    sh = gc.open_by_key(settings.sheet_catabase_id)
    ws = sh.worksheet("TCB Pics Formatted")
    
    all_rows = ws.get_all_values()
    if not all_rows:
        print("[error] Sheet is empty")
        return
    
    header = all_rows[0]
    data_rows = all_rows[1:]
    
    if args.limit:
        data_rows = data_rows[:args.limit]
    
    print(f"[info] Sheet has {len(data_rows)} data rows")
    
    #Build updates
    updates = build_import_data(data_rows, detector_labels, classifier_labels, cutoff_serial)
    print(f"[info] Found {len(updates)} rows to update")
    
    if args.dry_run:
        print("\n=== DRY RUN - Preview of first 20 updates ===")
        for row_idx, box_coords, box_cat_ids in updates[:20]:
            sn = data_rows[row_idx][COL_SERIAL] if data_rows[row_idx] else "?"
            print(f"  Row {row_idx + 2}: {sn}")
            print(f"    BoxCoords: {box_coords[:80]}..." if len(box_coords) > 80 else f"    BoxCoords: {box_coords}")
            print(f"    BoxCatIDs: {box_cat_ids[:80]}..." if len(box_cat_ids) > 80 else f"    BoxCatIDs: {box_cat_ids}")
        return
    
    #Batch write to sheet
    if args.commit:
        print("\n=== COMMITTING CHANGES ===")
        
        #Build cell updates
        cells_to_update = []
        for row_idx, box_coords, box_cat_ids in updates:
            sheet_row = row_idx + 2  #Convert to 1-indexed, skip header
            if box_coords:
                cells_to_update.append({
                    "range": f"I{sheet_row}",
                    "values": [[box_coords]]
                })
            if box_cat_ids:
                cells_to_update.append({
                    "range": f"J{sheet_row}",
                    "values": [[box_cat_ids]]
                })
        
        #Batch update in chunks to avoid API limits
        import time
        chunk_size = 100
        for i in range(0, len(cells_to_update), chunk_size):
            chunk = cells_to_update[i:i + chunk_size]
            ws.batch_update(chunk)
            print(f"  Updated {min(i + chunk_size, len(cells_to_update))}/{len(cells_to_update)} cells...")
            #Throttle to stay under 60 writes/minute quota
            if i + chunk_size < len(cells_to_update):
                time.sleep(2)
        
        print(f"\n[done] Updated {len(updates)} rows")


if __name__ == "__main__":
    main()
