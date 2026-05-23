# R6 Training Notebooks

Colab notebooks that produce the next-gen CV models for TomCat on Modal T4.

## Inputs (shared by both notebooks)

Upload one zip to Drive root (`/content/drive/MyDrive/`):

```
R6_TomCat_Training.zip
├── TomCatBot Pics.csv         (any *.csv with "pic" in the name; from repo root)
└── TotalPicsOfCats/           (any folder with "totalpics" in the name)
    ├── sn0001.jpg
    └── ...                     (everything in cache/PicsOfCats/Pictures/)
```

To build from the repo:
1. Copy `cache/PicsOfCats/Pictures/` to a folder named `TotalPicsOfCats/`
2. Copy `TomCatBot Pics.csv` next to it
3. Zip both → `R6_TomCat_Training.zip`
4. Upload to `My Drive/`

~16 GB upload (JPEG doesn't compress further). Once in Drive both notebooks reuse the same zip.

## Notebooks

### `R6_cat_DINOv3_vit_l.ipynb` — Re-ID encoder

- Backbone: DINOv3 ViT-L/16 (1024 features → 512 embedding)
- Loss: ArcFace, R5's Optuna-tuned margin/scale baked in (no search)
- 3-phase schedule: frozen warmup (3 ep) → finetune (30 ep, patience=8) → polish (5 ep @ 0.25× LR)
- Resume-from-checkpoint built in (Colab disconnects don't lose progress)
- Outputs in `My Drive/R6_cat_DINOv3/`:
  - `R6_cat_DINOv3_encoder.pth` ← production weight
  - `R6_cat_DINOv3_best.pt`, `_last.pt`, `_final.pt` (full ckpts w/ optimizer state)
  - `R6_cat_DINOv3_config.json`, `_classes.json`, `_metrics.csv`

A100 40GB runtime: ~4-5 hours.

### `R6_yolo12l.ipynb` — Detector

- Model: YOLO12l (26M params, ~50 MB)
- Hyperparameter tuning: Ultralytics `model.tune()` (50 GA iterations × 20 epochs)
- Final training: 300 epochs with best mutation, cosine LR, patience=50
- Eval includes recall sweep at the bot's actual conf threshold (0.552)
- Outputs in `My Drive/R6_yolo12l/`:
  - `R6_cat_yolo12l.pt` ← production weight
  - `final/weights/best.pt`, `last.pt` (training run)
  - `tune/best_hyperparameters.yaml`

A100 40GB runtime: ~6 hours (tune ~4h + final ~2h).

## After both finish — bot-side swap

Download both weights into `weights/`:
- `R6_cat_DINOv3_encoder.pth`
- `R6_cat_yolo12l.pt`

Then update three places:

1. **`tomcat/vision/vision.py`** (around line 591) — change backbone name and head input dim:
   ```python
   self.backbone = timm.create_model('vit_large_patch16_dinov3', pretrained=True, num_classes=0)
   self.head = nn.Sequential(nn.Linear(1024, 512, bias=True), nn.BatchNorm1d(512), nn.PReLU())
   ```

2. **`cloud/modal/cv_inference.py`** (around line 115) — mirror the same change in the Modal-side wrapper, then update the local-weight defaults:
   ```python
   _DEFAULT_LOCAL_YOLO    = _REPO_ROOT / "weights" / "R6_cat_yolo12l.pt"
   _DEFAULT_LOCAL_ENCODER = _REPO_ROOT / "weights" / "R6_cat_DINOv3_encoder.pth"
   ```

3. **`modal deploy cloud/modal/cv_inference.py`** to push the new image. ViT-L adds ~700 MB to the image; first cold start will be slower one time.

4. Trigger a gallery retrain via the bot UI — generates `R6_cat_DINOv3_gallery.pt` from the new encoder. Until that finishes, the bot will fall back to the old gallery (mismatched dim) and identifies will break, so do this immediately after deploy.

## Notes

- **`MIN_SAMPLES=8`** in the encoder notebook drops cats with fewer than 8 crops. With current data that's ~52 of 140 cats — they'll be invisible to training but can still be added back to the gallery later as one-shot entries.
- **`Rejected` boxes** are skipped for both training tasks (R5 convention).
- **Multi-cat rows** (`Eraser|Eggs` style) are handled: encoder gets one crop per cat, detector gets multiple boxes on one image.
- **Test recall at conf=0.552 specifically** when validating the detector — that's the threshold the bot uses in production ([cv_inference.py:223](../cloud/modal/cv_inference.py:223)).
