"""Pluggable CV inference backends.

Abstracts WHERE heavy CV models run. The host process always owns the gallery
state and post-processing; only the GPU forward passes are routed.

Backends:
- LocalBackend: in-process, GPU on the host machine.
- ModalBackend: remote GPU via a deployed Modal app (see cloud/modal/cv_inference.py).

The boundary is the `detect_and_embed` method: one image in, boxes + DINOv3
embeddings out. That's the only thing the hot Discord identify path needs and
it travels as a single round-trip for Modal.

The older `detect` / `embed_tensors` methods are kept for in-process paths
that aren't worth refactoring yet (rerank, batch retrain, labeler). They only
work on LocalBackend; ModalBackend raises NotImplementedError on them. When
CV_BACKEND=modal, the main identify call works but rerank silently degrades.

Selected by settings.cv_backend (env CV_BACKEND). Default 'local'.
"""

from __future__ import annotations

import asyncio
import io
import threading
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from PIL import Image
from torch import Tensor

if TYPE_CHECKING:
    from .vision import Det


# Modal app and class names. Match what cloud/modal/cv_inference.py deploys.
MODAL_APP_NAME = "tomcat-cv"
MODAL_CLASS_NAME = "CVInference"


class CVBackend(ABC):
    @property
    def supports_local_tensor_embed(self) -> bool:
        """True if embed_tensors works in-process without a network round-trip.

        Hot-path code uses this to decide whether to enable rerank (which is
        chatty and only sensible against a local encoder).
        """
        return False

    def preload(self) -> None:
        """Load any local state this backend needs. Called at bot startup.

        Default: load the gallery (always local). Subclasses extend.
        """
        from . import vision as V

        V._ensure_gallery()

    @abstractmethod
    def detect_and_embed(
        self,
        image_bytes: bytes,
        *,
        conf: float,
        detect_imgsz: int,
        pad_pct: float,
    ) -> Dict[str, Any]:
        """One-shot YOLO12 + crop + DINOv3 embed. The hot identify path.

        Returns:
            {
                'image_size': (w, h),
                'detections': [
                    {
                        'box': (x1, y1, x2, y2),        # detector box
                        'crop_box': (cx1, cy1, cx2, cy2),  # padded crop box used
                        'conf': float,
                        'embedding': list[float],         # length 512
                    },
                    ...
                ],
            }
        """

    @abstractmethod
    def detect(self, img: Image.Image) -> List["Det"]:
        """Run YOLO detection only. Used by non-identify paths."""

    @abstractmethod
    def embed_tensors(self, batch: Tensor) -> Tensor:
        """DINOv3 forward on a preprocessed [N, 3, H, W] tensor.

        Local-only convenience for paths that haven't been refactored to
        embed_crops yet (gallery retrain). ModalBackend raises here so misuse
        is loud — sending raw tensors over the wire would be wasteful.
        """

    @abstractmethod
    def embed_crops(self, crops: List[Image.Image]) -> Tensor:
        """DINOv3 forward on a list of PIL crops. Returns [N, D] CPU tensor.

        Preferred over embed_tensors for any path that has PIL crops in hand:
        the backend owns preprocessing (resize/normalize) so ModalBackend can
        ship compact JPEG bytes instead of multi-megabyte raw tensors.
        """

    @abstractmethod
    def sam_refine_crop(self, crop_bytes: bytes, prompt_box: List[float]) -> Any:
        """Run SAM2 on one crop with one box prompt.

        Returns a numpy [N, H, W] uint8/bool array of candidate masks. The
        caller applies guard-rail selection logic locally so this interface
        stays narrow (raw masks only).
        """


class LocalBackend(CVBackend):
    """Runs models in-process using the loaders defined in vision.py."""

    @property
    def supports_local_tensor_embed(self) -> bool:
        return True

    def preload(self) -> None:
        from . import vision as V

        V._ensure_detector()
        V._ensure_encoder()
        V._ensure_gallery()

    def detect(self, img: Image.Image) -> List["Det"]:
        from . import vision as V

        V._ensure_detector()
        with V._yolo_lock:
            res = V._yolo.predict(
                img,
                conf=V.settings.cv_conf or V._DEFAULT_CONF,
                imgsz=V.settings.cv_detect_imgsz,
                verbose=False,
            )
        dets: List["Det"] = []
        for r in res:
            boxes = r.boxes.xyxy.detach().cpu().numpy()
            confs = r.boxes.conf.detach().cpu().numpy()
            for b, c in zip(boxes, confs):
                dets.append(
                    V.Det(
                        (float(b[0]), float(b[1]), float(b[2]), float(b[3])),
                        float(c),
                    )
                )
        return dets

    def embed_tensors(self, batch: Tensor) -> Tensor:
        import torch

        from . import vision as V

        V._ensure_encoder()
        if V._clf is None:
            raise RuntimeError(
                "DINOv3 encoder failed to load; see prior vision logs"
            )
        with torch.inference_mode():
            with V._clf_lock:
                return V._clf(batch)

    def embed_crops(self, crops: List[Image.Image]) -> Tensor:
        import contextlib

        import torch

        from . import vision as V

        if not crops:
            return torch.empty((0, 512))
        V._ensure_encoder()
        V._ensure_device_only()
        if V._clf is None:
            raise RuntimeError(
                "DINOv3 encoder failed to load; see prior vision logs"
            )
        batch = torch.stack([V._prep_tensor(c) for c in crops]).to(V._device)
        # fp16 autocast when the encoder is on CUDA — matches the speed the
        # gallery retrain used to get from its own _load_encoder + autocast loop.
        autocast_ctx = (
            torch.autocast(device_type="cuda", dtype=torch.float16)
            if (V._half and V._device is not None and V._device.type == "cuda")
            else contextlib.nullcontext()
        )
        with torch.inference_mode():
            with V._clf_lock:
                with autocast_ctx:
                    return V._clf(batch)

    def sam_refine_crop(self, crop_bytes: bytes, prompt_box: List[float]) -> Any:
        import numpy as np
        import torch
        from PIL import Image

        from . import vision as V

        V._ensure_sam()
        if V._sam is None:
            return np.zeros((0, 0, 0), dtype=bool)
        crop = np.asarray(Image.open(io.BytesIO(crop_bytes)).convert("RGB"))
        with torch.inference_mode():
            results = V._sam(crop, bboxes=[list(prompt_box)], verbose=False)
        return V._extract_sam_masks(results)

    def detect_and_embed(
        self,
        image_bytes: bytes,
        *,
        conf: float,
        detect_imgsz: int,
        pad_pct: float,
    ) -> Dict[str, Any]:
        import torch

        from . import vision as V

        V._ensure_encoder()
        img = V._open_rgb_image(io.BytesIO(image_bytes))
        img_w, img_h = img.size

        V._ensure_detector()
        with V._yolo_lock:
            yres = V._yolo.predict(
                img, conf=conf, imgsz=detect_imgsz, verbose=False
            )

        det_records: List[Dict[str, Any]] = []
        crops: List[Image.Image] = []
        for r in yres:
            boxes = r.boxes.xyxy.detach().cpu().numpy()
            confs = r.boxes.conf.detach().cpu().numpy()
            for b, c in zip(boxes, confs):
                x1, y1, x2, y2 = (float(b[0]), float(b[1]), float(b[2]), float(b[3]))
                cx1, cy1, cx2, cy2 = V._expand_box(x1, y1, x2, y2, pad_pct, img_w, img_h)
                crops.append(img.crop((cx1, cy1, cx2, cy2)))
                det_records.append(
                    {
                        "box": (x1, y1, x2, y2),
                        "crop_box": (cx1, cy1, cx2, cy2),
                        "conf": float(c),
                    }
                )

        if not crops:
            return {"image_size": (img_w, img_h), "detections": []}

        batch = torch.stack([V._prep_tensor(c) for c in crops]).to(V._device)
        with torch.inference_mode():
            with V._clf_lock:
                embs = V._clf(batch).detach().cpu().tolist()
        for rec, emb in zip(det_records, embs):
            rec["embedding"] = emb

        return {"image_size": (img_w, img_h), "detections": det_records}


class ModalBackend(CVBackend):
    """Calls a deployed Modal app for CV inference.

    Lazy connection: the modal.Cls handle is resolved on first use, not at
    construction. That keeps `import` cheap and lets the bot start even if
    Modal credentials aren't configured (LocalBackend would still work after
    a CV_BACKEND flip).
    """

    def __init__(self) -> None:
        self._cls = None
        self._instance = None
        self._connect_lock = threading.Lock()

    def preload(self) -> None:
        # Gallery only; encoder/detector live on Modal.
        from . import vision as V

        V._ensure_gallery()
        # Warm a container at startup so the first user call hits a hot path.
        # Failures here are non-fatal — the bot can still run, the first
        # identify just pays the cold-start cost.
        try:
            self._ensure_connected()
            self.ping()
        except Exception:
            pass

    def _ensure_connected(self) -> None:
        if self._instance is not None:
            return
        with self._connect_lock:
            if self._instance is not None:
                return
            import modal

            self._cls = modal.Cls.from_name(MODAL_APP_NAME, MODAL_CLASS_NAME)
            self._instance = self._cls()

    def detect_and_embed(
        self,
        image_bytes: bytes,
        *,
        conf: float,
        detect_imgsz: int,
        pad_pct: float,
    ) -> Dict[str, Any]:
        self._ensure_connected()
        return self._instance.detect_and_embed.remote(
            image_bytes,
            conf=conf,
            detect_imgsz=detect_imgsz,
            pad_pct=pad_pct,
        )

    def detect(self, img: Image.Image) -> List["Det"]:
        # No separate detect-only endpoint deployed; route through
        # detect_and_embed and discard the embeddings. Costs ~50ms of wasted
        # DINOv3 work server-side but avoids a Modal redeploy. The labeler
        # "Detect" button is the main caller and isn't latency-critical.
        from . import vision as V
        from ..config import settings

        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=92)
        result = self.detect_and_embed(
            buf.getvalue(),
            conf=float(settings.cv_conf or V._DEFAULT_CONF),
            detect_imgsz=int(settings.cv_detect_imgsz),
            pad_pct=float(settings.cv_pad_pct),
        )
        return [
            V.Det(tuple(d["box"]), float(d["conf"]))
            for d in (result.get("detections") or [])
        ]

    def embed_tensors(self, batch: Tensor) -> Tensor:
        raise NotImplementedError(
            "ModalBackend.embed_tensors is not implemented — sending raw "
            "tensors over the wire is wasteful. Use embed_crops(PIL images) "
            "instead. Gallery retrain still routes through here and needs "
            "Phase 3c work to switch."
        )

    def embed_crops(self, crops: List[Image.Image]) -> Tensor:
        import torch

        if not crops:
            return torch.empty((0, 512))
        self._ensure_connected()
        crop_bytes: List[bytes] = []
        for c in crops:
            buf = io.BytesIO()
            # JPEG quality 92 matches detect_and_embed; small bandwidth cost,
            # negligible quality cost for DINOv3 embedding.
            c.convert("RGB").save(buf, format="JPEG", quality=92)
            crop_bytes.append(buf.getvalue())
        embs = self._instance.embed_crops.remote(crop_bytes)
        return torch.tensor(embs, dtype=torch.float32)

    def sam_refine_crop(self, crop_bytes: bytes, prompt_box: List[float]) -> Any:
        import numpy as np
        from PIL import Image

        self._ensure_connected()
        try:
            mask_pngs = self._instance.sam_refine_crop.remote(
                crop_bytes, list(prompt_box)
            )
        except Exception:
            return np.zeros((0, 0, 0), dtype=bool)
        if not mask_pngs:
            return np.zeros((0, 0, 0), dtype=bool)
        masks = []
        for png in mask_pngs:
            try:
                m = np.asarray(Image.open(io.BytesIO(png)).convert("L"))
                masks.append(m > 127)
            except Exception:
                continue
        if not masks:
            return np.zeros((0, 0, 0), dtype=bool)
        return np.stack(masks, axis=0)

    def ping(self) -> Dict[str, Any]:
        """Hit the deployed app's ping endpoint to keep a container warm."""
        self._ensure_connected()
        return self._instance.ping.remote()


_backend: Optional[CVBackend] = None
_backend_lock = threading.Lock()


def get_backend() -> CVBackend:
    global _backend
    if _backend is not None:
        return _backend
    with _backend_lock:
        if _backend is not None:
            return _backend
        from ..config import settings

        name = str(settings.cv_backend or "local").strip().lower()
        if name == "local":
            _backend = LocalBackend()
        elif name == "modal":
            _backend = ModalBackend()
        else:
            raise ValueError(
                f"Unknown CV_BACKEND={name!r}. Use 'local' or 'modal'."
            )
        return _backend


def reset_backend() -> None:
    """Clear the cached backend so the next get_backend() re-reads settings."""
    global _backend
    with _backend_lock:
        _backend = None


# ---------- Activity-window keep-warm for ModalBackend ----------
#
# After each user CV request, callers invoke notify_modal_activity(). That
# resets a monotonic "last_active" timestamp and (if not already running)
# spawns a background coroutine that pings Modal every keep_warm_sec until
# the activity window expires. The container stays warm during bursts of
# use and dies on its own afterward (Modal scaledown_window applies once we
# stop pinging), so cost scales with active session time, not 24/7.

_keep_warm_last_active: float = 0.0
_keep_warm_task: Optional["asyncio.Task[None]"] = None
_keep_warm_async_lock: Optional[asyncio.Lock] = None


async def notify_modal_activity() -> None:
    """Mark a CV request as just-happened. Starts/extends the keep-warm window.

    Safe to call regardless of backend or settings — no-ops when not Modal or
    when keep-warm is disabled.
    """
    global _keep_warm_last_active, _keep_warm_task, _keep_warm_async_lock

    from ..config import settings

    interval = int(getattr(settings, "cv_modal_keep_warm_sec", 0) or 0)
    if interval <= 0:
        return
    backend = get_backend()
    if not isinstance(backend, ModalBackend):
        return

    _keep_warm_last_active = time.monotonic()

    if _keep_warm_async_lock is None:
        _keep_warm_async_lock = asyncio.Lock()

    async with _keep_warm_async_lock:
        if _keep_warm_task is not None and not _keep_warm_task.done():
            return
        window = int(getattr(settings, "cv_modal_activity_window_sec", 0) or 0)
        _keep_warm_task = asyncio.create_task(
            _keep_warm_loop(backend, interval, window)
        )


async def _keep_warm_loop(
    backend: "ModalBackend", interval_sec: int, window_sec: int
) -> None:
    """Ping Modal every interval_sec until the activity window expires.

    window_sec=0 means always-on (never expire). The first iteration sleeps
    before pinging so a single request doesn't immediately cause an extra
    ping on top of the request itself.
    """
    from ..logger import log_action

    try:
        log_action(
            "modal_keep_warm_started",
            f"interval={interval_sec}s; window={window_sec or 'always'}",
            "ok",
        )
        while True:
            await asyncio.sleep(interval_sec)
            if window_sec > 0:
                elapsed = time.monotonic() - _keep_warm_last_active
                if elapsed >= window_sec:
                    break
            try:
                await asyncio.to_thread(backend.ping)
            except Exception as e:
                log_action(
                    "modal_keep_warm_error", f"err={type(e).__name__}", str(e)
                )
    except asyncio.CancelledError:
        raise
    finally:
        log_action("modal_keep_warm_stopped", "", "ok")
