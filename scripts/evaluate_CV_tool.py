import asyncio
import csv
import io
import sys
import os
from pathlib import Path
import numpy as np
from PIL import Image

#Get the absolute path to the pytomcat root directory (one level up from scripts/)
project_root = Path(__file__).resolve().parent.parent

#Change the current working directory to the root so relative paths work correctly
os.chdir(project_root)

#Add the root directory to Python's import path so 'from tomcat...' works
sys.path.insert(0, str(project_root))

# Expanded rerank augmentation: nine rotation angles, with mirroring disabled by default.
os.environ["LABELER_RERANK_ANGLES"] = "-20,-15,-10,-5,0,5,10,15,20"
# Rerank the top 100 candidates for a deeper second pass.
os.environ["LABELER_RERANK_TOP_N"] = "100"
os.environ["LABELER_RERANK_HFLIP"] = "0"
os.environ["LABELER_RERANK_ENABLED"] = "1"

# Import project modules once environment overrides are set.
from tomcat.vision import vision
from tomcat.config import settings
from tomcat.services import local_photos

# Gallery checkpoint to evaluate.
settings.cv_gallery_path = "weights/R4.5.4_cat_DINOv3_gallery.pt"

#Quality filter thresholds (override via env if needed)
MIN_CROP_SIDE = int(os.getenv("EVAL_MIN_CROP_SIDE", "96") or "96")
MIN_CROP_AREA = int(os.getenv("EVAL_MIN_CROP_AREA", "9216") or "9216")
MIN_BLUR_SCORE = float(os.getenv("EVAL_MIN_LAPLACIAN_VAR", "45.0") or "45.0")


def _label_is_rejected(label: str) -> bool:
    low = str(label or "").strip().lower().replace(" ", "")
    return low in {"rejected", "needsreview", "review"}


def _label_is_notacat(label: str) -> bool:
    low = str(label or "").strip().lower().replace(" ", "")
    return low == "notacat"


def _laplacian_var(gray: np.ndarray) -> float:
    if gray.ndim != 2 or gray.shape[0] < 3 or gray.shape[1] < 3:
        return 0.0
    center = gray[1:-1, 1:-1]
    lap = (
        (-4.0 * center)
        + gray[:-2, 1:-1]
        + gray[2:, 1:-1]
        + gray[1:-1, :-2]
        + gray[1:-1, 2:]
    )
    return float(np.var(lap))


def _box_to_xyxy(box, img_w: int, img_h: int):
    cx, cy, w, h = box
    x1 = int((cx - (w / 2.0)) * img_w)
    y1 = int((cy - (h / 2.0)) * img_h)
    x2 = int((cx + (w / 2.0)) * img_w)
    y2 = int((cy + (h / 2.0)) * img_h)
    x1 = max(0, min(img_w - 1, x1))
    y1 = max(0, min(img_h - 1, y1))
    x2 = max(1, min(img_w, x2))
    y2 = max(1, min(img_h, y2))
    if x2 <= x1 or y2 <= y1:
        return None
    return x1, y1, x2, y2


def _quality_fail_reason(crop: Image.Image) -> str | None:
    w, h = crop.size
    if w < MIN_CROP_SIDE or h < MIN_CROP_SIDE or (w * h) < MIN_CROP_AREA:
        return "low_res"
    gray = np.asarray(crop.convert("L"), dtype=np.float32)
    if _laplacian_var(gray) < MIN_BLUR_SCORE:
        return "blurry"
    return None


async def main():
    print("Loading DINOv3 Classifier...")
    #Initialize the classifier and gallery only (detector is bypassed)
    vision._ensure_classifier()
    print(
        f"Quality filters -> min_side={MIN_CROP_SIDE}px, "
        f"min_area={MIN_CROP_AREA}px^2, min_laplacian_var={MIN_BLUR_SCORE:.1f}"
    )
    
    cat_list = vision.get_all_cats()
    cat_map = {c.lower(): c for c in cat_list}
    
    print("\nGathering Serial Numbers already embedded in the gallery...")
    gallery_sns = set()
    
    #By accessing vision._gallery_paths, we get the populated list
    for path in vision._gallery_paths:
        sn, _ = vision._parse_serial_crop_from_path(path)
        if sn is not None:
            gallery_sns.add(sn)
            
    print(f"Found {len(gallery_sns)} unique Serial Numbers currently in R4.5.3.")
    
    test_cases = []
    csv_path = local_photos.metadata_csv_path()
    
    total_rows = 0
    holdout_rows = 0
    holdout_crops = 0
    rows_skipped_box_id_mismatch = 0
    rows_skipped_missing_ids = 0
    rows_skipped_rejected = 0
    crops_skipped_rejected = 0
    crops_skipped_notacat = 0
    crops_skipped_unknown_label = 0
    crops_skipped_invalid_box = 0

    print("\nParsing Catabase for holdout boxed crops...")
    with open(csv_path, 'r', encoding='utf-8') as f:
        reader = csv.reader(f)
        next(reader)
        for row in reader:
            if len(row) < 9:
                continue

            sn_str = row[6]
            box_coords = row[7]
            box_cats = row[8]
            
            if not sn_str.strip().isdigit():
                continue
            sn = int(sn_str.strip())
            total_rows += 1
            
            #GATE 1: Must NOT be in the gallery already
            if sn in gallery_sns:
                continue
                
            #GATE 2: Skip explicitly rejected/invalid rows
            if "reject" in box_coords.lower():
                rows_skipped_rejected += 1
                continue
                
            #GATE 3: Parse crops + labels from CSV in order
            coords = [c.strip() for c in box_coords.split("|") if c.strip()]
            if not coords:
                continue

            #Require explicit per-box IDs and count parity with boxes
            if not box_cats.strip():
                rows_skipped_missing_ids += 1
                continue
            labels_raw = [l.strip() for l in box_cats.split("|")]
            if len(labels_raw) != len(coords):
                rows_skipped_box_id_mismatch += 1
                continue

            labeled_crops = []
            for coord_str, raw_label in zip(coords, labels_raw):
                if _label_is_rejected(raw_label):
                    crops_skipped_rejected += 1
                    continue
                if _label_is_notacat(raw_label):
                    crops_skipped_notacat += 1
                    continue
                box = vision._parse_yolo_box_str(coord_str)
                if box is None:
                    crops_skipped_invalid_box += 1
                    continue
                cx, cy, w, h = box
                if w <= 0 or h <= 0:
                    crops_skipped_invalid_box += 1
                    continue
                norm = vision._normalize_cat_label(raw_label, cat_map)
                if not norm:
                    crops_skipped_unknown_label += 1
                    continue
                labeled_crops.append((box, norm))

            if not labeled_crops:
                continue

            holdout_rows += 1
            holdout_crops += len(labeled_crops)
            test_cases.append({
                'sn': sn,
                'crops': labeled_crops,
            })
            
    print(f"Identified {holdout_rows} valid holdout photos ({holdout_crops} labeled crops) out of {total_rows} total rows.")
    print(
        "Skipped rows -> "
        f"missing IDs: {rows_skipped_missing_ids}, "
        f"box/ID mismatch: {rows_skipped_box_id_mismatch}, "
        f"rejected rows: {rows_skipped_rejected}"
    )
    print(
        "Skipped crops (label/box parse) -> "
        f"rejected/review: {crops_skipped_rejected}, "
        f"NotACat: {crops_skipped_notacat}, "
        f"unknown labels: {crops_skipped_unknown_label}, "
        f"invalid boxes: {crops_skipped_invalid_box}"
    )
    
    correct_r1 = 0
    correct_r5 = 0
    total_crops = 0
    processed_count = 0
    skipped_low_res = 0
    skipped_blurry = 0
    skipped_no_prediction = 0
    failed_download_or_decode = 0

    # Lower this if GPU memory is limited.
    concurrency_limit = 6
    sem = asyncio.Semaphore(concurrency_limit)

    async def process_single_case(tc):
        async with sem:
            img_bytes = await asyncio.to_thread(local_photos.read_local_photo_bytes, tc['sn'])
            if not img_bytes:
                return {
                    "targets": [],
                    "result": None,
                    "low_res": 0,
                    "blurry": 0,
                    "failed": 1,
                }

            try:
                img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
            except Exception:
                return {
                    "targets": [],
                    "result": None,
                    "low_res": 0,
                    "blurry": 0,
                    "failed": 1,
                }

            img_w, img_h = img.size
            kept_targets = []
            low_res_count = 0
            blurry_count = 0
            for box, true_label in tc['crops']:
                xyxy = _box_to_xyxy(box, img_w, img_h)
                if xyxy is None:
                    low_res_count += 1
                    continue
                crop = img.crop(xyxy)
                reason = _quality_fail_reason(crop)
                if reason == "low_res":
                    low_res_count += 1
                    continue
                if reason == "blurry":
                    blurry_count += 1
                    continue
                kept_targets.append((box, true_label))

            if not kept_targets:
                return {
                    "targets": [],
                    "result": None,
                    "low_res": low_res_count,
                    "blurry": blurry_count,
                    "failed": 0,
                }

            boxes = [b for b, _ in kept_targets]
            #Classifier-only path on provided CSV boxes (detector bypassed).
            result = await asyncio.to_thread(
                vision.identify_boxes,
                img_bytes,
                boxes,
                top_k=5,
                refs_per=0,
                include_ref_thumbs=False,
            )
            return {
                "targets": kept_targets,
                "result": result,
                "low_res": low_res_count,
                "blurry": blurry_count,
                "failed": 0,
            }

    print(f"\nStarting Classifier-Only Crop Evaluation with {concurrency_limit}x Concurrency...")
    
    tasks = [process_single_case(tc) for tc in test_cases]
    
    for task in asyncio.as_completed(tasks):
        res = await task
        processed_count += 1

        skipped_low_res += int(res.get("low_res", 0))
        skipped_blurry += int(res.get("blurry", 0))
        failed_download_or_decode += int(res.get("failed", 0))

        targets = res.get("targets", [])
        result = res.get("result")
        if not targets or result is None:
            if processed_count % 10 == 0 or processed_count == len(test_cases):
                acc_r1 = (correct_r1 / total_crops * 100) if total_crops > 0 else 0
                acc_r5 = (correct_r5 / total_crops * 100) if total_crops > 0 else 0
                print(f"[{processed_count}/{len(test_cases)}] | Crops Tested: {total_crops} | R@1: {acc_r1:.1f}% | R@5: {acc_r5:.1f}%")
            continue

        preds = result.results or []
        total_crops += len(targets)

        for i, (_, true_label) in enumerate(targets):
            if i >= len(preds):
                skipped_no_prediction += 1
                continue
            cand_rows = preds[i].get("candidates", [])
            if not cand_rows:
                skipped_no_prediction += 1
                continue
            top5 = [str(c.get("name", "")) for c in cand_rows[:5]]
            pred_name = top5[0] if top5 else ""

            if pred_name == true_label:
                correct_r1 += 1
            if true_label in top5:
                correct_r5 += 1
                    
        if processed_count % 10 == 0 or processed_count == len(test_cases):
            acc_r1 = (correct_r1 / total_crops * 100) if total_crops > 0 else 0
            acc_r5 = (correct_r5 / total_crops * 100) if total_crops > 0 else 0
            print(f"[{processed_count}/{len(test_cases)}] | Crops Tested: {total_crops} | R@1: {acc_r1:.1f}% | R@5: {acc_r5:.1f}%")

    print("\n" + "="*45)
    print("           FINAL TEST RESULTS")
    print("="*45)
    print(f"Holdout Images Tested   : {len(test_cases)}")
    print(f"Total Labeled Crops     : {total_crops}")
    print(f"Correct Identifications : {correct_r1}")
    if total_crops > 0:
        print(f"Overall Accuracy (R@1)  : {correct_r1/total_crops*100:.2f}%")
        print(f"Top-5 Accuracy (R@5)    : {correct_r5/total_crops*100:.2f}%")
    print(
        "Skipped crops (quality) : "
        f"low_res={skipped_low_res}, blurry={skipped_blurry}, no_pred={skipped_no_prediction}"
    )
    print(f"Failed image loads      : {failed_download_or_decode}")
    print("="*45)

if __name__ == "__main__":
    asyncio.run(main())
