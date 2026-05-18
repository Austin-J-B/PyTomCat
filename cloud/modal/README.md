# Modal CV deployment

Runs YOLO12 detection and DINOv3 embedding on a Modal T4 GPU so the bot
process doesn't need one. Gallery match stays on the bot side.

## Prerequisites

- A Modal account (`pip install modal`, then `modal setup` from an activated venv).
- The encoder `.pth` and detector `.pt` exist in `weights/` locally — the
  deploy step uploads them into the container image.

## Deploy

From the repo root, with the venv active:

```
modal deploy cloud/modal/cv_inference.py
```

First deploy will build the image (~5 min, mostly torch + ultralytics).
Subsequent deploys reuse the cached image and just push code changes.

Override weight paths if your files don't match the defaults:

```
set CV_LOCAL_YOLO_PATH=weights/984_917_yolo12s.pt
set CV_LOCAL_ENCODER_PATH=weights/R5_cat_DINOv3_encoder.pth
modal deploy cloud/modal/cv_inference.py
```

## Smoke test

After deploy, time a real round-trip with a local image:

```
modal run cloud/modal/cv_inference.py --image-path path\to\some_cat.jpg
```

Expect roughly:
- First call (cold/snapshot-restore): 2-6 s
- Warm call: <1 s end-to-end (Modal RTT + GPU inference)

## Bot wiring

In the bot's `.env`:

```
CV_BACKEND=modal
```

The bot's `ModalBackend` (see `tomcat/vision/backend.py`) will call this app's
`CVInference.detect_and_embed`. Until then, leave `CV_BACKEND=local`.

## Cost notes

- T4 GPU: ~$0.59/hr while a container is running.
- `scaledown_window=600` keeps a container warm for 10 min after the last
  request — within a session, all calls hit a warm GPU.
- `max_containers=2` caps fan-out so a burst doesn't multiply cost.
- Bursty hobbyist usage (a few dozen identifies/day clustered into sessions)
  should land well inside the $30/mo Modal credit. Watch the dashboard for
  the first week.

## What's NOT on Modal (yet)

- **Gallery retrain** (`tomcat/services/gallery_updater.py`) — still local.
  Will route through `embed_crops` here in a later phase.
- **SAM2 box refinement** (labeler tool) — still local. Tool is admin-only
  and not latency-critical.
