import cv2
import os
import random
import shutil
import numpy as np
import torch
import ctypes  
from ultralytics import YOLO, SAM

# ================= CONFIGURATION =================
# PORTS
YOLO_OLD_PATH = r"C:\Users\austi\Documents\TomCat VI Training\Weights\intermediateModels\976_879_yolo11nP2.pt"
YOLO_NEW_PATH = r"C:\Users\austi\Documents\TomCat VI Training\DetectorModelTraining\SecondFullSmallTrain(BEST)\weights\best.pt"
SAM_PATH      = r"C:\Users\austi\Documents\TomCat VI Training\Weights\SAM\sam2_s.pt"

# DIRECTORIES
IMG_DIR_SOURCE = r"C:\Users\austi\Downloads\TomCatSupplement\raw"
BASE_SAVE_DIR  = r"C:\Users\austi\Downloads\TomCatSupplement\supplement"

LABELS_DIR   = os.path.join(BASE_SAVE_DIR, "labels")
REJECTED_DIR = os.path.join(BASE_SAVE_DIR, "rejected")
INVALID_DIR  = os.path.join(BASE_SAVE_DIR, "invalid")

# SETTINGS
CONF_THRESH_OLD = 0.35
CONF_THRESH_NEW = 0.50
HUD_OPACITY = 0.6
MASK_COLOR = (0, 0, 255) 
MASK_ALPHA = 0.2
MOVE_SPEED = 4  

# KEY CODES (Windows)
KEY_UP    = 2490368 
KEY_DOWN  = 2621440 
KEY_LEFT  = 2424832 
KEY_RIGHT = 2555904 

# STATE
CURRENT_MODE = "UNLABELED" # Options: "UNLABELED", "REJECTED"

# ================= INPUT POLLING SETUP =================
def is_key_down(key_code):
    return ctypes.windll.user32.GetAsyncKeyState(key_code) & 0x8000 != 0

VK_W, VK_A, VK_S, VK_D = 0x57, 0x41, 0x53, 0x44
VK_UP, VK_DOWN, VK_LEFT, VK_RIGHT = 0x26, 0x28, 0x25, 0x27

# ================= SETUP =================
for d in [LABELS_DIR, REJECTED_DIR, INVALID_DIR]:
    os.makedirs(d, exist_ok=True)

print(f"Loading Models...")
yolo_old = YOLO(YOLO_OLD_PATH)
yolo_new = YOLO(YOLO_NEW_PATH) 
sam_model = SAM(SAM_PATH)

cv2.namedWindow("TomCat Labeler", cv2.WINDOW_NORMAL)
cv2.resizeWindow("TomCat Labeler", 1280, 720)

session_history = [] 
history_ptr = -1 

# ================= HELPER FUNCTIONS =================
def get_detailed_stats():
    total_source = len([f for f in os.listdir(IMG_DIR_SOURCE) if f.lower().endswith(('.jpg', '.png'))])
    n_accepted = len([f for f in os.listdir(LABELS_DIR) if f.lower().endswith('.txt')])
    n_rejected = len([f for f in os.listdir(REJECTED_DIR) if f.lower().endswith(('.jpg', '.png'))])
    n_invalid  = len([f for f in os.listdir(INVALID_DIR) if f.lower().endswith(('.jpg', '.png'))])
    n_resolved = n_accepted + n_rejected + n_invalid
    return {
        "total": total_source, "resolved": n_resolved,
        "accepted": n_accepted, "rejected": n_rejected, "invalid": n_invalid
    }

def get_next_image(mode):
    if mode == "UNLABELED":
        all_source = [f for f in os.listdir(IMG_DIR_SOURCE) if f.lower().endswith(('.jpg', '.png'))]
        candidates = []
        for f in all_source:
            txt_exists = os.path.exists(os.path.join(LABELS_DIR, os.path.splitext(f)[0] + ".txt"))
            is_rej = os.path.exists(os.path.join(REJECTED_DIR, f))
            is_inv = os.path.exists(os.path.join(INVALID_DIR, f))
            if not txt_exists and not is_rej and not is_inv:
                candidates.append(os.path.join(IMG_DIR_SOURCE, f))
        if candidates: return random.choice(candidates), False

    elif mode == "REJECTED":
        all_rejected = [f for f in os.listdir(REJECTED_DIR) if f.lower().endswith(('.jpg', '.png'))]
        rescue_candidates = []
        for f in all_rejected:
            txt_exists = os.path.exists(os.path.join(LABELS_DIR, os.path.splitext(f)[0] + ".txt"))
            is_inv = os.path.exists(os.path.join(INVALID_DIR, f))
            if not txt_exists and not is_inv:
                rescue_candidates.append(os.path.join(REJECTED_DIR, f))
        if rescue_candidates: return random.choice(rescue_candidates), True
    
    # CRITICAL FIX: Return None instead of (None, False) to prevent unpacking errors
    return None

def ensemble_detect(img):
    res_new = yolo_new(img, conf=CONF_THRESH_NEW, verbose=False)[0]
    boxes_new = res_new.boxes.xyxy.cpu().numpy().tolist() if res_new.boxes else []
    res_old = yolo_old(img, conf=CONF_THRESH_OLD, verbose=False)[0]
    boxes_old = res_old.boxes.xyxy.cpu().numpy().tolist() if res_old.boxes else []
    final_boxes = list(boxes_new)
    for b_old in boxes_old:
        is_duplicate = False
        for b_new in boxes_new:
            if calculate_iou(b_old, b_new) > 0.5: is_duplicate = True; break
        if not is_duplicate: final_boxes.append(expand_box(b_old, img.shape, 0.1))
    return final_boxes

def calculate_iou(boxA, boxB):
    xA = max(boxA[0], boxB[0]); yA = max(boxA[1], boxB[1])
    xB = min(boxA[2], boxB[2]); yB = min(boxA[3], boxB[3])
    interArea = max(0, xB - xA) * max(0, yB - yA)
    boxAArea = (boxA[2] - boxA[0]) * (boxA[3] - boxA[1])
    boxBArea = (boxB[2] - boxB[0]) * (boxB[3] - boxB[1])
    unionArea = boxAArea + boxBArea - interArea
    return interArea / unionArea if unionArea > 0 else 0

def expand_box(box, img_shape, percent=0.1):
    h, w = img_shape[:2]
    x1, y1, x2, y2 = box
    bw, bh = x2 - x1, y2 - y1
    return [max(0, x1 - bw*percent), max(0, y1 - bh*percent),
            min(w, x2 + bw*percent), min(h, y2 + bh*percent)]

def get_sam_box(img, prompt_box):
    results = sam_model(img, bboxes=[prompt_box], verbose=False)
    if results and results[0].masks:
        mask = results[0].masks.data[0].cpu().numpy().astype(bool)
        h, w = mask.shape[-2:]
        rows = np.any(mask, axis=1); cols = np.any(mask, axis=0)
        if not np.any(rows) or not np.any(cols): return prompt_box, mask
        rmin, rmax = np.where(rows)[0][[0, -1]]
        cmin, cmax = np.where(cols)[0][[0, -1]]
        return [max(0, cmin-5), max(0, rmin-5), min(w, cmax+5), min(h, rmax+5)], mask
    return prompt_box, None

def clamp_box(box, max_w, max_h):
    return [max(0, min(max_w, box[0])), max(0, min(max_h, box[1])), 
            max(0, min(max_w, box[2])), max(0, min(max_h, box[3]))]

# ================= RENDERING =================
def render_static_background(img, masks, win_w, win_h, img_name, mode):
    h, w = img.shape[:2]
    scale = min(win_w / w, win_h / h)
    new_w, new_h = int(w * scale), int(h * scale)
    pad_x = (win_w - new_w) // 2
    pad_y = (win_h - new_h) // 2
    
    resized_img = cv2.resize(img, (new_w, new_h))
    canvas = np.zeros((win_h, win_w, 3), dtype=np.uint8)
    canvas[pad_y:pad_y+new_h, pad_x:pad_x+new_w] = resized_img
    
    if masks:
        combined_mask = np.any(np.stack(masks), axis=0)
        mask_resized = cv2.resize(combined_mask.astype(np.uint8), (new_w, new_h), interpolation=cv2.INTER_NEAREST).astype(bool)
        colored_overlay = resized_img.copy()
        colored_overlay[mask_resized] = MASK_COLOR
        roi = canvas[pad_y:pad_y+new_h, pad_x:pad_x+new_w]
        cv2.addWeighted(colored_overlay, MASK_ALPHA, roi, 1 - MASK_ALPHA, 0, roi)
        
    stats = get_detailed_stats()
    info_lines = [
        f"File: {img_name}",
        f"Type: {mode}",
        f"Resolved: {stats['resolved']} / {stats['total']}",
        f"Accepted: {stats['accepted']}",
        f"Rejected: {stats['rejected']}",
        f"Invalid:  {stats['invalid']}"
    ]
    
    overlay = canvas.copy()
    cv2.rectangle(overlay, (0, 0), (300, 140), (0,0,0), -1)
    bar_h = 40
    cv2.rectangle(overlay, (0, win_h - bar_h), (win_w, win_h), (0,0,0), -1)
    cv2.addWeighted(overlay, HUD_OPACITY, canvas, 1 - HUD_OPACITY, 0, canvas)
    
    font = cv2.FONT_HERSHEY_SIMPLEX
    white = (255, 255, 255)
    y = 20
    for line in info_lines:
        cv2.putText(canvas, line, (10, y), font, 0.4, white, 1)
        y += 20
    
    instr = "Y:Save N:Reject | 0:Swap Mode | WASD: Top-Left | Arrows: Bot-Right | 2:Add X:Del E:Fix"
    cv2.putText(canvas, instr, (10, win_h - 12), font, 0.4, white, 1)
    return canvas, scale, pad_x, pad_y

# ================= MAIN LOOP =================
while True:
    # 1. NAVIGATION LOGIC
    if history_ptr < len(session_history) - 1:
        history_ptr += 1
        current_data = session_history[history_ptr]
        img_path, is_rescue = current_data['path'], current_data['is_rescue']
        final_boxes, masks = current_data['boxes'], current_data['masks']
        
        # --- RESTORATION PROTOCOL (Fix for crash on Undo) ---
        if not os.path.exists(img_path):
            fname = os.path.basename(img_path)
            possible_locs = [
                os.path.join(INVALID_DIR, fname),
                os.path.join(REJECTED_DIR, fname),
                os.path.join(IMG_DIR_SOURCE, fname)
            ]
            restored = False
            for loc in possible_locs:
                if os.path.exists(loc):
                    shutil.move(loc, img_path)
                    txt_p = os.path.join(LABELS_DIR, os.path.splitext(fname)[0] + ".txt")
                    if os.path.exists(txt_p): os.remove(txt_p)
                    restored = True
                    break
            if not restored:
                print(f"CRITICAL: Could not find {fname} to restore! Skipping history entry.")
                continue 

    else:
        # --- NEW IMAGE LOADING & AUTO-SWITCH ---
        res = get_next_image(CURRENT_MODE)
        
        # If current mode is empty, try the other mode automatically
        if res is None:
            if CURRENT_MODE == "UNLABELED":
                print("Unlabeled folder empty. Switching to REJECTED mode...")
                CURRENT_MODE = "REJECTED"
                res = get_next_image(CURRENT_MODE)
            
        # If STILL None, we are truly done
        if res is None: 
            print(f"No images left in UNLABELED or REJECTED folders. Exiting.")
            break
            
        img_path, is_rescue = res
        history_ptr += 1
        session_history.append({'path': img_path, 'is_rescue': is_rescue, 'boxes': None, 'masks': None})
        final_boxes = None; masks = None

    img_name = os.path.basename(img_path)
    img = cv2.imread(img_path)
    if img is None: continue
    h, w = img.shape[:2]

    if final_boxes is None:
        raw_boxes = ensemble_detect(img)
        final_boxes = []; masks = []
        for b in raw_boxes:
            refined_box, mask = get_sam_box(img, b)
            final_boxes.append(refined_box)
            if mask is not None: masks.append(mask)
        if not final_boxes: final_boxes = [[w*0.25, h*0.25, w*0.75, h*0.75]]
        session_history[history_ptr]['boxes'] = final_boxes
        session_history[history_ptr]['masks'] = masks

    active_box_idx = 0
    decision = None 

    cached_bg = None; cached_scale = 1.0; cached_pad_x = 0; cached_pad_y = 0
    last_win_size = (0,0); force_rerender = True 

    while True:
        try: _, _, win_w, win_h = cv2.getWindowImageRect("TomCat Labeler")
        except: win_w, win_h = 1280, 720
        
        if force_rerender or (win_w, win_h) != last_win_size:
            mode_text = "UNLABELED" if CURRENT_MODE == "UNLABELED" else "REJECTED (Rescue)"
            cached_bg, cached_scale, cached_pad_x, cached_pad_y = render_static_background(
                img, masks, win_w, win_h, img_name, mode_text
            )
            last_win_size = (win_w, win_h)
            force_rerender = False

        display = cached_bg.copy()
        for i, b in enumerate(final_boxes):
            color = (0, 255, 0) if i == active_box_idx else (0, 100, 0)
            thick = 2 if i == active_box_idx else 1
            sx1 = int(b[0] * cached_scale) + cached_pad_x
            sy1 = int(b[1] * cached_scale) + cached_pad_y
            sx2 = int(b[2] * cached_scale) + cached_pad_x
            sy2 = int(b[3] * cached_scale) + cached_pad_y
            cv2.rectangle(display, (sx1, sy1), (sx2, sy2), color, thick)
            
        cv2.imshow("TomCat Labeler", display)
        k = cv2.waitKey(10) 
        
        if k == 27: decision = 'exit'; break
        elif k == 8: decision = 'back'; break
        elif k == 32: active_box_idx = (active_box_idx + 1) % len(final_boxes)
        elif k == ord('y'): decision = 'saved'; break
        elif k == ord('n'): decision = 'rejected'; break
        elif k == ord('0') or k == 48: decision = 'switch_mode'; break
            
        elif k == ord('2') or k == 50:
            cx, cy = w/2, h/2; bw, bh = w*0.2, h*0.2
            final_boxes.append([cx-bw, cy-bh, cx+bw, cy+bh])
            if masks: masks.append(np.zeros((h, w), dtype=bool))
            active_box_idx = len(final_boxes) - 1; force_rerender = True 
            
        elif k == ord('x') or k == 46:
            if len(final_boxes) > 0:
                final_boxes.pop(active_box_idx)
                if masks and len(masks) > active_box_idx: masks.pop(active_box_idx)
                active_box_idx = max(0, active_box_idx - 1); force_rerender = True 

        elif k == ord('e') and len(final_boxes) > 0:
            b = final_boxes[active_box_idx]
            expanded = expand_box(b, img.shape, 0.20)
            new_box, new_mask = get_sam_box(img, expanded)
            final_boxes[active_box_idx] = new_box
            if new_mask is not None:
                if active_box_idx < len(masks): masks[active_box_idx] = new_mask
                else: masks.append(new_mask)
            force_rerender = True 
        
        # D. DUAL CORNER MOVEMENT
        if len(final_boxes) > 0:
            b = final_boxes[active_box_idx]
            
            if is_key_down(VK_W): b[1] -= MOVE_SPEED  # Up
            if is_key_down(VK_S): b[1] += MOVE_SPEED  # Down
            if is_key_down(VK_A): b[0] -= MOVE_SPEED  # Left
            if is_key_down(VK_D): b[0] += MOVE_SPEED  # Right

            if is_key_down(VK_UP):   b[3] -= MOVE_SPEED # Up
            if is_key_down(VK_DOWN): b[3] += MOVE_SPEED # Down
            if is_key_down(VK_LEFT):  b[2] -= MOVE_SPEED # Left
            if is_key_down(VK_RIGHT): b[2] += MOVE_SPEED # Right

            if b[2] <= b[0] + 5: 
                if is_key_down(VK_LEFT): b[0] = b[2] - 5 
                else: b[2] = b[0] + 5
            if b[3] <= b[1] + 5:
                if is_key_down(VK_UP): b[1] = b[3] - 5
                else: b[3] = b[1] + 5

            final_boxes[active_box_idx] = clamp_box(final_boxes[active_box_idx], w, h)
        session_history[history_ptr]['boxes'] = final_boxes
        session_history[history_ptr]['masks'] = masks

    # 4. DECISION LOGIC
    txt_path = os.path.join(LABELS_DIR, os.path.splitext(img_name)[0] + ".txt")
    rej_path = os.path.join(REJECTED_DIR, img_name)
    inv_path = os.path.join(INVALID_DIR, img_name)

    if decision == 'exit': break
    elif decision == 'back': history_ptr = max(-1, history_ptr - 2); continue
    elif decision == 'switch_mode':
        if CURRENT_MODE == "UNLABELED": CURRENT_MODE = "REJECTED"
        else: CURRENT_MODE = "UNLABELED"
        continue 
        
    elif decision in ['saved', 'rejected']:
        # 1. ACTION (Do this FIRST)
        if decision == 'saved':
            if final_boxes:
                with open(txt_path, 'w') as f:
                    for b in final_boxes:
                        cx = ((b[0] + b[2]) / 2) / w
                        cy = ((b[1] + b[3]) / 2) / h
                        bw = (b[2] - b[0]) / w
                        bh = (b[3] - b[1]) / h
                        cx, cy, bw, bh = [min(max(x, 0), 1) for x in [cx, cy, bw, bh]]
                        f.write(f"0 {cx:.6f} {cy:.6f} {bw:.6f} {bh:.6f}\n")
                
                # FIX: If we just saved a Rescue image, MOVE it to Source so it exists!
                if is_rescue:
                    shutil.move(img_path, os.path.join(IMG_DIR_SOURCE, img_name))

        elif decision == 'rejected':
            dest = INVALID_DIR if is_rescue else REJECTED_DIR
            shutil.copy(img_path, os.path.join(dest, img_name))

        # 2. CLEANUP (Do this LAST)
        if decision == 'saved':
            if os.path.exists(rej_path): os.remove(rej_path)
            if os.path.exists(inv_path): os.remove(inv_path)
        elif decision == 'rejected':
            if is_rescue:
                if os.path.exists(rej_path): os.remove(rej_path)
            else:
                if os.path.exists(inv_path): os.remove(inv_path)

cv2.destroyAllWindows()