# TomCat Image Labeling Pipeline - Implementation TODO

> **Created:** 2026-01-30  
> **Status:** Planning Complete - Awaiting Execution  
> **Context:** Web-based HITL labeling system for cat identification

---

## Quick Reference

| Item | Value |
|------|-------|
| Sheet | TCB Pics Formatted |
| New Columns | I: BoxCoordinates, J: BoxCatIDs, K: OfficerComments (shifted) |
| Gallery Base | `weights/R4.5_cat_DINOv3_gallery.pt` (7,316 embeddings, 92 cats) |
| Gallery Updates | `R4.5.1_cat_DINOv3_gallery.pt`, `R4.5.2_...`, etc. |
| Quality Filter | ≥122,500 pixels (350p) + ≥4 images per cat |
| Crop Padding | 5% |

---

## Architecture Overview

```
Website Labeling System
├── Tab 1: Detector       → Draw/adjust bounding boxes
├── Tab 2: Classifier     → Pick cat from top-9 DINOv3 predictions
├── Tab 3: Manual Review  → Dropdown for NeedsReview crops + Add New Cat
└── Tab 4: Cat Gallery    → Grid view of labeled crops by cat name

Discord Integration
├── "TomCat identify" → ✅ reaction → Cache crop for 4 AM gallery update
└── "TomCat identify" → ❌ reaction → Queue for Manual Review tab
```

---

## Execution Phases

### Phase 1: Infrastructure
- [x] Update `install.py` to add SAM2 weights download
- [x] Add BoxCoordinates (I) and BoxCatIDs (J) columns to sheet schema
- [x] Extend `vision.py` with SAM loader and `detect_with_sam()`

### Phase 2: Import Existing Labels
- [x] Create `scripts/import_labels.py`
  - Find cutoff serial (highest in PreviousDetectorLabels)
  - Parse .txt files for BoxCoordinates
  - Parse known_cats/ and HITL_labeled_crops/ for BoxCatIDs
  - Mark omitted serials ≤ cutoff as "Rejected"
  - Mark "0. NotACat" rows as "Rejected"

### Phase 3: Backend API
- [x] Create `handlers/labeler.py`
  - GET  /api/labeler/queue/detect (serials needing boxes)
  - GET  /api/labeler/queue/classify (serials with incomplete labels)
  - GET  /api/labeler/image/{sn} (image + annotations)
  - POST /api/labeler/detect (YOLO+SAM → boxes)
  - POST /api/labeler/identify (DINOv3 → top-N)
  - POST /api/labeler/save (batch write to sheet)
  - GET  /api/labeler/cats (dropdown list)
- [x] Register routes in `main.py`

### Phase 4: Gallery Update Pipeline
- [ ] Create `scripts/gallery_updater.py`
  - Batch process new crops (100 at a time)
  - Apply 350p + 4-image quality filter
  - Run DINOv3 encoder → 512-dim embeddings
  - Append to existing gallery (incremental update)
  - Save as R4.5.{N+1}_cat_DINOv3_gallery.pt
  - Post Discord notification to logging channel

### Phase 5: Frontend UI
- [x] Create `labeler.js` (root folder)
  - Canvas with image display
  - Keyboard shortcuts (WASD, arrows, 1-9, 0, X, Enter, Backspace, Tab)
  - API integration for queue, detect, identify, save
  - Batch pending updates
- [x] Add dark theme CSS to `index.html`
- [x] Add Detector/Classifier tabs via mode switcher
- [x] Add labeler view to VIEWS array and setView()
- [x] Officer-only access control

### Phase 6: Discord Feedback Integration
- [ ] Update `tomcat/handlers/vision.py` to process ✅/❌ reactions
  - ✅ → Cache crop + box + predicted cat to `cache/discord_verified/`
  - ❌ → Queue to `cache/discord_disputed/` for Manual Review
- [ ] At 4 AM, include verified crops in gallery update HIGH PRIORITY
- [ ] Sub feature of the above. Consider if it will be resource effective to have a 'refresh entire gallery' feature for when we find previous labels that were incorrect.
    - For example, if, during classifier labeling, we see one image is incorrect, we can go back and update it for the proper label. Since this wouldn't update the gallery with the 4am schedule, we may need to update the entire gallery instead of just adding to it/updating it. If this isn't resource intensive in any concerning amount, we can just have the normal button/feature update the entire thing instead of topping it off.

---

## Cell Value Semantics

### BoxCoordinates (Column I)
| Value | Meaning |
|-------|---------|
| (empty) | Not yet labeled |
| `Rejected` | No valid cats in image |
| `0.5 0.3 0.4 0.6` | Single box (cx cy w h) |
| `0.5 0.3 0.4 0.6\|0.2 0.7 0.3 0.2` | Multiple boxes, pipe-separated |

### BoxCatIDs (Column J)
| Value | Meaning |
|-------|---------|
| (empty) | Not yet classified |
| `Twix` | Single cat |
| `Twix\|Hershey` | Multiple cats, matching box order |
| `Twix\|Rejected` | First box is Twix, second is invalid |
| `NeedsReview` | Unknown cat, requires manual selection |

---

## Keyboard Shortcuts

### Detector Tab
| Key | Action |
|-----|--------|
| WASD | Nudge top-left corner |
| Arrows | Nudge bottom-right corner |
| 2 | Add new box |
| X | Delete selected box |
| E | Run SAM refinement |
| Y/Enter | Save, advance |
| N | Reject image |
| Backspace | Undo |

### Classifier Tab
| Key | Action |
|-----|--------|
| 1-9 | Select prediction |
| 0 | Mark NeedsReview |
| X | Reject crop |
| Enter | Confirm, advance |
| Backspace | Undo |

---

## Discord Notification Format

After 4 AM gallery update:
```
📊 Gallery updated to R4.5.3
Added 127 embeddings for 45 cats
Total: 7,443 embeddings across 94 cats
```

---

## Future Enhancements (Phase 7+)
- [ ] Full CatDatabase editor (location, description, birthday, etc.)
- [ ] Bulk label correction tool
- [ ] Training run scheduler for detector (if ever needed)
- [ ] Mobile-friendly labeling interface
- [ ] Update the column A 'CatID' column to include the names/IDs of the cats identified in BoxCatIDs column J
