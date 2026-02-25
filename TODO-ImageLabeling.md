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
- [x] Create `scripts/gallery_updater.py`
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
- [x] Add detector/classifier background warm loop and prefetch cache
- [x] Add first-load detector warm gate (10 ready minimum from first 25)
- [x] Add loading/progress overlay for warm-up and catch-up waits
- [x] Show gallery retrain controls only in classifier mode
- [x] Performance hardening (no feature cuts)
  - Keep full detector behavior (`/detect` uses YOLO+SAM; no downgrade to detect-only mode)
  - Remove redundant SAM re-refine on first detect result to avoid duplicate work
  - Add transient API retry handling in `labeler.js` for 421/429/5xx + network timeout failures
  - Keep prefetch retries conservative to avoid overload while foreground requests recover
  - Add backend short-TTL response caches for detect/refine/identify to reuse repeated serial+box requests
  - Add classifier warm progress overlay while prediction options are loading
  - Mitigate classifier 429 bursts with prefetch backoff + in-flight reuse
  - Raise labeler API rate-limit headroom (per-user buckets, higher cap for cached image fetches)

### Phase 6: Discord Feedback Integration
- [x] Update `tomcat/handlers/vision.py` to process check/cross reactions
  - check -> Cache crop + predicted cat to `cache/discord/correct/` and write auto-labels to sheet
  - cross -> Cache to `cache/discord/incorrect/` and clear labels in sheet so it re-enters normal detector/classifier flow
  - Every identify image is upserted into `TCB Pics Formatted` immediately, so next 4 AM rebuilds include it from sheet history.
  - Reactions are persisted from CV identify replies via `tomcat/services/vision_feedback.py`.
- [x] (phase 4) At 4 AM, include verified crops in gallery update HIGH PRIORITY
  - 4 AM gallery rebuild now ingests `cache/discord/correct/records` and includes these crops even when min-per-cat would otherwise filter them out.
- [x] Sub feature of the above. Consider if it will be resource effective to have a 'refresh entire gallery' feature for when we find previous labels that were incorrect.
    - Gallery retrain now always runs a full rebuild (`run_gallery_update` coerces mode to `full`), so corrected historical labels are picked up automatically on the next run.

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
- [ ] Add a small/targeted gallery update path for corrected labels
  - Keep the nightly full rebuild as the source of truth.
  - Add an optional fast path that re-embeds only recently corrected/flagged serials, then refreshes those entries in the active gallery.
  - Ensure this still handles older corrected labels (from incorrect-label flags) without requiring users to wait for the next full sweep.
- [ ] Full CatDatabase editor (location, description, birthday, etc.)
  - If not feasible/pretty/realistic, add a 'new cat' button with a simple dropdown of like "name, physical description, location(s)" that integrates with the CatDatabase tab.
  -Currently considering if this makes sense as an option. it may just be easier to use the google sheet itself and add a new row there since there are less than 200 currently. If we wanted to 'add new cat' in the website, we'd have to figure out a way to drag/drop the rows of the catabase and also update the data validation.

- [x] Update column A `CatID` from classifier labels in column J using CatDatabase ID-name format
- [x] Build Manual Review tab (lighter all-cat reviewer for `NeedsReview` crops)
    - Added `Manual Review` mode + `/api/labeler/queue/manual` for rows containing `NeedsReview`
    - Added lightweight manual cache path (default `LABELER_MANUAL_REF_PER_CAT=50`) + warm/status endpoints
    - Added all-cat candidate panel with one representative ref image per cat and Cat ID display
    - Added manual search box (ID/name) that auto-scrolls/highlights matching cat cards
    - Added click-to-select labeling flow for manual mode (numeric selection intentionally disabled)
    - Moved manual search into fixed header so it stays visible while manual candidate cards scroll
    - Manual keyboard behavior: `Enter` in search input runs find; global `Enter` now defers to next photo
    - Added keyed manual candidate cache + ahead-of-time prefetch for upcoming manual queue photos
- [x] Add a 'incorrect label' flag button. 
  - Cursor toggles into red-flag mode (minesweeper-style) while active.
  - Clicking a reference photo clears prior sheet labels for that serial (CatID + box columns) so it re-enters normal labeling flow.

