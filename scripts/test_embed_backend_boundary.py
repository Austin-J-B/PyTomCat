"""Every consumer of a backend's embeddings must accept what it actually returns.

The two backends return different things, for good reason: LocalBackend has just
run the encoder and holds torch tensors, while ModalBackend receives lists of
floats over the wire and has no reason to import torch to wrap them -- that
import costs 358MB in a process that runs no model.

That makes the boundary a trap, and it caught me: when ModalBackend switched to
numpy, two callers kept calling torch methods on the result and would have
raised AttributeError the first time they ran.

  * gallery_updater ran torch.nn.functional.normalize on it, so a retrain on
    the Modal backend died -- and retrains are scheduled with
    required_backend="modal".
  * vision._embed_query_from_box called .numel() and .detach(), which is the
    labeler's manual-review path.

Neither is reachable from the test suite without a GPU or a network, so this
drives the same code with a stub backend instead.

Run: python scripts/test_embed_backend_boundary.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _test_support

import numpy as np

#A MagicMock stands in for torch on a bare checkout, and it answers every call,
#so `import torch` succeeding proves nothing. The parts of this that need real
#tensors ask _test_support what it had to fake.
HAVE_TORCH = "torch" not in _test_support.STUBBED

FAILURES: List[str] = []


def check(label: str, expected, actual) -> None:
    if expected == actual:
        print(f"  PASS {label}")
    else:
        FAILURES.append(label)
        print(f"  FAIL {label}\n       expected {expected!r}\n       got      {actual!r}")


def ok(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS {label}")
    else:
        FAILURES.append(label)
        print(f"  FAIL {label}{('  -> ' + detail) if detail else ''}")


def test_modal_fast_paths() -> None:
    """Box-only detection avoids DINO, while SAM precision stays configurable."""
    from PIL import Image

    from tomcat.config import settings
    from tomcat.vision.backend import ModalBackend

    class RemoteMethod:
        def __init__(self, fn):
            self.remote = fn

    class Instance:
        pass

    backend = ModalBackend()
    backend._instance = Instance()
    calls = {}
    backend._instance.detect_only = RemoteMethod(
        lambda image_bytes, **kwargs: {
            "detections": [{"box": (1.0, 2.0, 30.0, 40.0), "conf": 0.9}]
        }
    )
    backend._instance.detect_and_embed = RemoteMethod(
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("DINO should be skipped"))
    )
    image = Image.new("RGB", (64, 48))
    detections = backend.detect(image)
    check("Modal detect uses YOLO-only boxes", [((1.0, 2.0, 30.0, 40.0), 0.9)],
          [(d.xyxy, d.conf) for d in detections])

    previous_half = settings.cv_half
    try:
        settings.cv_half = True

        def new_sam_api(crop_bytes, prompt_box, **kwargs):
            calls["half"] = kwargs.get("half")
            return []

        backend._instance.sam_refine_crop = RemoteMethod(
            new_sam_api
        )
        empty = backend.sam_refine_crop(b"", [0.0, 0.0, 1.0, 1.0])
        check("Modal SAM receives configured fp16 mode", True, calls.get("half"))
        check("empty remote masks stay empty", (0, 0, 0), empty.shape)

        old_api_calls = []

        def old_sam_api(crop_bytes, prompt_box, **kwargs):
            old_api_calls.append(dict(kwargs))
            if "half" in kwargs:
                raise TypeError("got an unexpected keyword argument 'half'")
            return []

        backend._instance.sam_refine_crop = RemoteMethod(old_sam_api)
        backend.sam_refine_crop(b"", [0.0, 0.0, 1.0, 1.0])
        check("older Modal SAM signatures retry without the optional flag", [{"half": True}, {}], old_api_calls)
    finally:
        settings.cv_half = previous_half

    legacy = Instance()
    legacy_calls = []
    legacy.detect_only = RemoteMethod(
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("remote method detect_only not found in deployed app")
        )
    )
    legacy.detect_and_embed = RemoteMethod(
        lambda image_bytes, **kwargs: legacy_calls.append(kwargs) or {"detections": []}
    )
    backend._instance = legacy
    check("older Modal apps retain the detect fallback", [], backend.detect(image))
    ok("legacy detect fallback keeps its settings", bool(legacy_calls))


def main() -> int:
    print("=" * 70)
    print("embedding backend boundary tests")
    print("=" * 70)

    from tomcat.vision import vision as V

    print("\n[1] _as_embeddings takes whatever a backend hands it")
    #Modal's shape: a list of lists straight off the wire.
    out = V._as_embeddings([[1.0, 2.0], [3.0, 4.0]])
    check("a list of lists", (2, 2), out.shape)
    check("becomes float32", "float32", str(out.dtype))
    out = V._as_embeddings(np.zeros((3, 512), dtype=np.float64))
    check("a float64 array is narrowed", "float32", str(out.dtype))
    check("a single vector gains a row axis", (1, 4),
          V._as_embeddings(np.zeros(4, dtype=np.float32)).shape)

    if not HAVE_TORCH:
        print("  SKIP torch not installed, cannot check the tensor path")
    else:
        import torch

        #Local's shape: a tensor, possibly fp16 from autocast.
        out = V._as_embeddings(torch.zeros((2, 512), dtype=torch.float16))
        check("a torch tensor", (2, 512), out.shape)
        check("and is narrowed to float32", "float32", str(out.dtype))

    print("\n[2] the retrain accepts either backend's embeddings")
    #This is the line a Modal retrain died on.
    from tomcat.services import gallery_updater as GU

    if not HAVE_TORCH:
        print("  SKIP torch not installed, the retrain needs it")
    else:
        import torch

        as_numpy = GU._as_cpu_tensor(np.ones((4, 512), dtype=np.float32))
        ok("numpy in, torch out", hasattr(as_numpy, "detach"))
        check("shape survives", (4, 512), tuple(as_numpy.shape))
        #The line a Modal retrain used to die on. float32 normalize lands a
        #hair under 1.0, so this is a tolerance rather than an equality.
        normalized = torch.nn.functional.normalize(as_numpy, p=2, dim=1)
        ok("and normalize works on it", abs(float(normalized[0].norm()) - 1.0) < 1e-6,
           f"norm={float(normalized[0].norm())!r}")

        as_tensor = GU._as_cpu_tensor(torch.ones((4, 512), dtype=torch.float16))
        ok("a tensor stays a tensor", hasattr(as_tensor, "detach"))
        check("and is float32 on the cpu", "torch.float32", str(as_tensor.dtype))

    print("\n[3] the manual-review query path returns something numpy can use")
    #_embed_query_from_box used to call .numel() and .detach() on this.
    real_embed = V._embed_crops
    V._embed_crops = lambda crops: np.ones((1, 512), dtype=np.float32)
    try:
        from PIL import Image
        import io as _io

        buf = _io.BytesIO()
        Image.new("RGB", (64, 64)).save(buf, format="JPEG")
        emb = V._embed_query_from_box(buf.getvalue(), (0.5, 0.5, 0.5, 0.5))
        ok("an embedding comes back", emb is not None)
        if emb is not None:
            check("one flat vector", (512,), np.asarray(emb).shape)
            #What manual_review_candidates does with it next.
            ok("and it multiplies against the gallery",
               float((np.ones((3, 512), np.float32) @ np.asarray(emb).reshape(-1))[0]) == 512.0)

        V._embed_crops = lambda crops: np.zeros((0, 512), dtype=np.float32)
        check("no crops means no embedding", None,
              V._embed_query_from_box(buf.getvalue(), (0.5, 0.5, 0.5, 0.5)))
    finally:
        V._embed_crops = real_embed

    print("\n[4] scoring works on a plain numpy gallery")
    #No file, no model: just the arithmetic every identify depends on.
    real_emb, real_names, real_idx = V._gallery_emb, V._gallery_names, V._gallery_cat_indices
    try:
        V._gallery_emb = V._l2_normalize(np.array(
            [[1.0, 0.0], [0.9, 0.1], [0.0, 1.0]], dtype=np.float32))
        V._gallery_names = ["Eraser", "Eraser", "Twix"]
        V._rebuild_gallery_cat_indices()
        check("one index array per cat", ["Eraser", "Twix"],
              sorted(V._gallery_cat_indices))
        rows = V._rank_unique_candidates_for_similarity(
            V._gallery_emb @ np.array([1.0, 0.0], dtype=np.float32), rerank=False)
        check("one row per cat, best first", ["Eraser", "Twix"], [r[0] for r in rows])
        ok("the nearer cat scores higher", rows[0][1] > rows[1][1])

        print("\n[5] ties break toward the lower index, every time")
        #torch.topk left this to BLAS accumulation order, so the labeler could
        #show a different reference crop for the same query between runs.
        tied = np.array([0.5, 0.5, 0.5, 0.5], dtype=np.float32)
        picks = {tuple(V._topk_desc(tied, 2)[1].tolist()) for _ in range(50)}
        check("same answer every time", {(0, 1)}, picks)
        values, indices = V._topk_desc(np.array([0.1, 0.9, 0.5], np.float32), 2)
        check("largest first", [1, 2], indices.tolist())
        check("with its values", [0.9, 0.5], [round(float(v), 3) for v in values])
        check("asking for more than exists", 3,
              int(V._topk_desc(np.zeros(3, np.float32), 99)[1].size))
        check("asking for none", 0, int(V._topk_desc(np.zeros(3, np.float32), 0)[1].size))
    finally:
        V._gallery_emb, V._gallery_names = real_emb, real_names
        V._gallery_cat_indices = real_idx

    print("\n[6] a zero row does not become nan")
    #torch's normalize clamps with eps; a nan here would poison every
    #similarity computed against that row.
    normed = V._l2_normalize(np.array([[0.0, 0.0], [3.0, 4.0]], dtype=np.float32))
    ok("no nan", not bool(np.isnan(normed).any()))
    check("and a real row is unit length", 1.0, round(float(np.linalg.norm(normed[1])), 6))

    print("\n[7] Modal fast paths and compatibility")
    test_modal_fast_paths()

    print("\n" + "=" * 70)
    if FAILURES:
        print(f"{len(FAILURES)} failure(s).")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
