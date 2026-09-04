"""Modal app: remote GPU CV inference for TomCat.

Hot path: `detect_and_embed(image_bytes)` runs YOLO12 + crop + DINOv3 in one
call and returns boxes + embeddings + the crop bytes (so rerank variants stay
on the server side too in a future revision).

Batch path: `embed_crops(crops_bytes_list)` for nightly gallery retrain.

Models are baked into the image at build time (small, ~365 MB total). Gallery
stays on the host (Oracle) — Modal returns raw embeddings only.

Deploy from repo root:
    modal deploy cloud/modal/cv_inference.py

The image build will upload the two weights files referenced by ENCODER_PATH
and YOLO_PATH below — make sure those files exist locally before deploying.
"""

from __future__ import annotations

import io
import warnings
from pathlib import Path
from typing import List

import modal

# Path inside the container where weights get baked.
WEIGHTS_DIR = "/opt/weights"
YOLO_PATH = f"{WEIGHTS_DIR}/yolo12s.pt"
ENCODER_PATH = f"{WEIGHTS_DIR}/dinov3_encoder.pth"
SAM_PATH = f"{WEIGHTS_DIR}/sam2_s.pt"


# Local weight resolution only runs when this module is imported on the deploy
# host (modal.is_local() == True). Inside a Modal container, __file__ is
# /root/cv_inference.py and the repo-root computation has nothing to resolve.
if modal.is_local():
    import os

    _REPO_ROOT = Path(__file__).resolve().parents[2]
    _DEFAULT_LOCAL_YOLO = _REPO_ROOT / "weights" / "984_917_yolo12s.pt"
    _DEFAULT_LOCAL_ENCODER = _REPO_ROOT / "weights" / "R6_cat_DINOv3_encoder.pth"
    _DEFAULT_LOCAL_SAM = _REPO_ROOT / "weights" / "sam2_s.pt"

    def _local_weight(env_name: str, default: Path) -> str:
        raw = os.getenv(env_name, "").strip()
        p = Path(raw) if raw else default
        if not p.is_absolute():
            p = _REPO_ROOT / p
        if not p.exists():
            raise FileNotFoundError(
                f"Required weight not found: {p}. Set {env_name} to point to it."
            )
        return str(p)

    _LOCAL_YOLO = _local_weight("CV_LOCAL_YOLO_PATH", _DEFAULT_LOCAL_YOLO)
    _LOCAL_ENCODER = _local_weight("CV_LOCAL_ENCODER_PATH", _DEFAULT_LOCAL_ENCODER)
    _LOCAL_SAM = _local_weight("CV_LOCAL_SAM_PATH", _DEFAULT_LOCAL_SAM)
else:
    _LOCAL_YOLO = ""
    _LOCAL_ENCODER = ""
    _LOCAL_SAM = ""


# Image: CUDA 12.1 torch + ultralytics (YOLO12) + timm (DINOv3 backbone).
# torch 2.8 pairs with torchvision 0.23. State_dicts are tensor data; minor
# torch version drift between host and container is fine for the encoder.
#
# libgl1 + libglib2.0-0 are required by opencv-python (pulled in transitively
# by ultralytics). debian_slim doesn't ship them. See
# https://github.com/ultralytics/ultralytics/issues/1270 — the headless OpenCV
# variant isn't officially supported by ultralytics yet, so we install the
# system libs instead.
_base_image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("libgl1", "libglib2.0-0")
    .pip_install(
        "torch==2.8.0",
        "torchvision==0.23.0",
        "ultralytics>=8.3.200",
        "timm>=1.0.11",
        "Pillow>=10.0",
        "numpy>=1.26,<2",
        extra_options="--extra-index-url https://download.pytorch.org/whl/cu121",
    )
)

# add_local_file is only meaningful on the deploy host; the container reuses
# the already-built image regardless of what the spec looks like at import.
if modal.is_local():
    image = (
        _base_image
        .add_local_file(_LOCAL_YOLO, YOLO_PATH, copy=True)
        .add_local_file(_LOCAL_ENCODER, ENCODER_PATH, copy=True)
        .add_local_file(_LOCAL_SAM, SAM_PATH, copy=True)
    )
else:
    image = _base_image


app = modal.App("tomcat-cv", image=image)


# Replicated from tomcat/vision/vision.py:DINOv3Wrapper so the Modal container
# can load the same .pth checkpoint without importing the bot package.
# If the bot's wrapper architecture changes, mirror it here.
def _build_encoder():
    import torch
    import timm

    class DINOv3Wrapper(torch.nn.Module):
        def __init__(self):
            super().__init__()
            # R6 ViT-L (was R5 ViT-B). Architecture must match tomcat/vision/vision.py:DINOv3Wrapper.
            self.backbone = timm.create_model(
                "vit_large_patch16_dinov3", pretrained=False, num_classes=0
            )
            self.head = torch.nn.Sequential(
                torch.nn.Linear(1024, 512, bias=True),
                torch.nn.BatchNorm1d(512),
                torch.nn.PReLU(),
            )

        def forward(self, x):
            with warnings.catch_warnings():
                warnings.filterwarnings(
                    "ignore", message=".*Torch was not compiled with flash attention.*"
                )
                feat = self.backbone(x)
            emb = self.head(feat)
            return torch.nn.functional.normalize(emb, p=2, dim=1)

    return DINOv3Wrapper()


@app.cls(
    gpu="t4",
    enable_memory_snapshot=True,
    # Container alive for 15 min after the last request. Gives back-to-back
    # identifies (labeling sessions, photo bursts) free warm-path latency
    # without paying the always-on idle cost. The bot's keep-warm pinger
    # (off by default) can extend this further during busy weeks.
    scaledown_window=900,
    timeout=180,
    max_containers=2,  # cap fan-out; bot is low-concurrency
)
class CVInference:
    # Per the Modal memory-snapshot pattern: load models to CPU during the
    # snap=True enter (gets snapshotted), then move to GPU after restore.
    # This makes cold starts ~1-3s instead of 15-40s.

    @modal.enter(snap=True)
    def load_on_cpu(self):
        import torch

        from ultralytics import YOLO, SAM

        # YOLO12 — ultralytics constructs lazily; force CPU at load time.
        self._yolo = YOLO(YOLO_PATH)
        try:
            self._yolo.model.to("cpu")
        except Exception:
            pass

        # SAM2 for labeler box refinement.
        self._sam = SAM(SAM_PATH)
        try:
            self._sam.model.to("cpu")
        except Exception:
            pass
        # Mirror the bot's workaround for ultralytics 8.3.249's missing
        # SAM2Model.warmup; patch in a no-op so prompted segmentation doesn't
        # break on first use.
        try:
            sam_model = getattr(self._sam, "model", None)
            if sam_model is not None and not hasattr(sam_model, "warmup"):
                setattr(sam_model, "warmup", lambda *args, **kwargs: None)
        except Exception:
            pass

        # DINOv3 encoder.
        encoder = _build_encoder()
        try:
            state = torch.load(ENCODER_PATH, map_location="cpu", weights_only=True)
        except Exception:
            state = torch.load(ENCODER_PATH, map_location="cpu")
        encoder.load_state_dict(state, strict=True)
        encoder.eval()
        self._encoder = encoder

        # ImageNet normalization tensors (built once, snapshotted).
        from torchvision.transforms import Compose, Normalize, Resize, ToTensor

        self._clf_imgsz = 448  # matches bot's settings.cv_clf_imgsz default
        self._prep = Compose(
            [
                Resize((self._clf_imgsz, self._clf_imgsz)),
                ToTensor(),
                Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
            ]
        )

    @modal.enter(snap=False)
    def move_to_gpu(self):
        import torch

        self._device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self._encoder = self._encoder.to(self._device).eval()
        try:
            self._yolo.model.to(self._device)
        except Exception:
            pass
        try:
            self._sam.model.to(self._device)
        except Exception:
            pass

    @modal.method()
    def detect_and_embed(
        self,
        image_bytes: bytes,
        *,
        conf: float = 0.552,
        detect_imgsz: int = 640,
        pad_pct: float = 0.03,
    ) -> dict:
        """Hot path: YOLO detect → crop → DINOv3 embed.

        Returns:
            {
                'image_size': (w, h),
                'detections': [
                    {
                        'box': (x1, y1, x2, y2),      # detector box (clipped)
                        'crop_box': (cx1, cy1, cx2, cy2),  # padded crop box used
                        'conf': float,
                        'embedding': list[float],      # normalized DINOv3 embedding
                    },
                    ...
                ],
            }
        """
        import torch
        from PIL import Image

        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        img_w, img_h = img.size

        results = self._yolo.predict(
            img, conf=conf, imgsz=detect_imgsz, verbose=False
        )

        det_records = []
        crops = []
        for r in results:
            boxes = r.boxes.xyxy.detach().cpu().numpy()
            confs = r.boxes.conf.detach().cpu().numpy()
            for b, c in zip(boxes, confs):
                x1, y1, x2, y2 = float(b[0]), float(b[1]), float(b[2]), float(b[3])
                cw, ch = x2 - x1, y2 - y1
                pw, ph = cw * pad_pct, ch * pad_pct
                cx1 = max(0.0, x1 - pw)
                cy1 = max(0.0, y1 - ph)
                cx2 = min(float(img_w), x2 + pw)
                cy2 = min(float(img_h), y2 + ph)
                crop = img.crop((cx1, cy1, cx2, cy2))
                crops.append(crop)
                det_records.append(
                    {
                        "box": (x1, y1, x2, y2),
                        "crop_box": (cx1, cy1, cx2, cy2),
                        "conf": float(c),
                    }
                )

        if not crops:
            return {"image_size": (img_w, img_h), "detections": []}

        batch = torch.stack([self._prep(c) for c in crops]).to(self._device)
        with torch.inference_mode():
            embs = self._encoder(batch).detach().cpu().tolist()
        for rec, emb in zip(det_records, embs):
            rec["embedding"] = emb

        return {"image_size": (img_w, img_h), "detections": det_records}

    @modal.method()
    def embed_crops(
        self,
        crops_bytes: List[bytes],
    ) -> List[List[float]]:
        """Batch path: embed already-cropped images. Used by gallery retrain."""
        import torch
        from PIL import Image

        if not crops_bytes:
            return []
        imgs = [Image.open(io.BytesIO(b)).convert("RGB") for b in crops_bytes]
        batch = torch.stack([self._prep(im) for im in imgs]).to(self._device)
        with torch.inference_mode():
            embs = self._encoder(batch).detach().cpu().tolist()
        return embs

    @modal.method()
    def sam_refine_crop(
        self,
        crop_bytes: bytes,
        prompt_box: List[float],
    ) -> List[bytes]:
        """Run SAM2 on one cropped image with one box prompt.

        Returns each candidate mask as PNG bytes (binary mask, 0/255). The
        caller decodes back to numpy and runs guard-rail logic locally — keeps
        the Modal surface narrow and avoids duplicating bot-side selection
        logic on the server.
        """
        import torch
        from PIL import Image

        img = Image.open(io.BytesIO(crop_bytes)).convert("RGB")

        with torch.inference_mode():
            results = self._sam(img, bboxes=[list(prompt_box)], verbose=False)

        out: List[bytes] = []
        for r in results:
            masks = getattr(r, "masks", None)
            if masks is None:
                continue
            data = getattr(masks, "data", None)
            if data is None:
                continue
            arr = data.detach().cpu().numpy()
            for i in range(arr.shape[0]):
                m = (arr[i] > 0.5).astype("uint8") * 255
                pil = Image.fromarray(m, mode="L")
                buf = io.BytesIO()
                pil.save(buf, format="PNG", optimize=False)
                out.append(buf.getvalue())
        return out

    @modal.method()
    def ping(self) -> dict:
        """Lightweight keep-warm endpoint. Returns load status, no GPU work."""
        import torch

        return {
            "ok": True,
            "device": str(self._device),
            "cuda_available": torch.cuda.is_available(),
            "encoder_revision": "R6",
        }


@app.local_entrypoint()
def smoke_test(image_path: str):
    """Run from CLI to sanity-check a deployed instance:

        modal run cloud/modal/cv_inference.py --image-path some_cat.jpg
    """
    import time

    data = Path(image_path).read_bytes()

    inst = CVInference()

    t0 = time.perf_counter()
    out = inst.detect_and_embed.remote(data)
    t1 = time.perf_counter()
    print(f"first call: {(t1 - t0) * 1000:.0f} ms")
    print(f"detections: {len(out['detections'])}")
    for i, d in enumerate(out["detections"]):
        print(
            f"  {i}: conf={d['conf']:.3f} box={tuple(round(v, 1) for v in d['box'])} "
            f"emb_dim={len(d['embedding'])}"
        )

    t2 = time.perf_counter()
    out2 = inst.detect_and_embed.remote(data)
    t3 = time.perf_counter()
    print(f"warm call: {(t3 - t2) * 1000:.0f} ms")

    t4 = time.perf_counter()
    inst.ping.remote()
    t5 = time.perf_counter()
    print(f"ping: {(t5 - t4) * 1000:.0f} ms")
