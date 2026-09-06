# Visuals and experiment provenance

All README images come from project screenshots or supplied training artifacts. No generative imagery was used. Discord copies were flattened and saved without source metadata; original files were left untouched.

| Repository asset | Supplied source / changes |
| --- | --- |
| `discord-photo.png` | `Screenshot 2026-09-05 222249.png`; human display name, timestamp, and avatar covered with opaque black pixels. Bot identity and command preserved. |
| `discord-identify.png` | Clipboard image `1b0db53d-8ba7-46ee-af7d-4c4cf3f4f545`; bot response only. The displayed percentage is a historical UI score, not a measured accuracy claim. |
| `discord-feeding.png` | Clipboard image `ed964d9d-3125-4c60-9dc0-9353d05a38c6`; all volunteer mentions covered with opaque pixels and neutral bars. |
| `features-sn3340.jpg`, `features-sn3510.jpg` | Matching JPEGs from `DetectorModelTraining/Heatmaps`; reduced to at most 1200 × 800 for the README. |
| `detector-pr.png`, `detector-training.png` | `BoxPR_curve.png` and `results.png` from `SecondFullSmallTrain(BEST)`; lossless image re-encoding. |
| `identity-projection.png` | Clipboard image `ee9839de-d4e7-4230-b1b8-f86763c72f62`; historical projection, method and checkpoint unknown. Legend entries are animal identities. |
| `identity-recall-full.png` | Clipboard image `fc1a50fd-b1ae-44cf-9a88-86a4c192a368`; historical full-validation plot. |
| `identity-recall-balanced.png` | Clipboard image `39a13890-5362-4662-a3c8-d2958d29bf61`; historical balanced-subset plot. |

## Detector numbers

`training/detector-results.csv` and `training/detector-args.yaml` are unchanged copies from the supplied run. The README selects the CSV row with maximum `metrics/mAP50-95(B)`, rather than combining independently best metrics across epochs.

At epoch 114, precision is 0.97503, recall 0.95816, mAP50 0.98402, and mAP50–95 0.91717. The CSV contains 125 epochs. The exported PR curve labels a single class, `cat`. These results do not evaluate individual identity recognition or the opossum example.

The supplied artifacts do not establish train/validation split independence, duplicate-image handling, or performance on a separate test set. The README reports the recorded validation result without making those additional claims.

## What the heatmaps show

The local `DetectorModelTraining/yolo_heatmaps.py` includes several CAM implementations, but its active main path calls `featuremap_heatmap`:

1. Stop the detector forward pass at `feature_break_idx` (saved setting: 4, zero-based).
2. Average the feature tensor across channels.
3. Min-max normalize that image's averaged map.
4. Apply the `viridis` color map and resize it for display.

The saved settings use `overlay_alpha=1`, so the output is fully color-mapped rather than blended with the photograph. They also use input upscaling and ImageNet normalization. This is an exploratory visualization path, not the production inference preprocessing.

No per-image run manifest ties these two JPEGs to that exact saved configuration. The script supports the feature-response interpretation, but the precise layer and settings for each export cannot be proven from the images alone. Colors are normalized independently, so brightness is not directly comparable between images.

Channel-averaged feature responses are not prediction-specific attribution. [Grad-CAM](https://arxiv.org/abs/1610.02391), by comparison, uses target gradients to produce a localization map for a prediction. The README deliberately avoids claiming that these maps explain an identity decision.

## Historical identity plots

The original plotting code, raw metrics, split definitions, projection method, and checkpoint IDs were not supplied with the clipboard figures. The plots are shown as experiment history. Approximate endpoint values are read from the charts, not reconstructed as raw measurements. No claim is made that the full-validation and balanced-subset curves belong to the same run, or that either measures the current encoder.
