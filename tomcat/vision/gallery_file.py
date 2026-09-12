"""Reading and writing the DINOv3 gallery, in either format.

The gallery is the only model artifact the host process needs when
CV_BACKEND=modal: detection and embedding happen on Modal, and what comes back
is scored against this locally. It holds one embedding per labelled crop --
(11799, 512) float32 at the time of writing -- plus the cat each one belongs
to and enough metadata to show the crop in the labeler.

It has always been a torch .pt, which means reading it costs an `import torch`:
~358MB resident and 2.5s, on a 4GB box that OOM-kills. Nothing in the file
needs torch; it is embeddings and labels. So .npz is the format now, and this
module reads either one:

  * .npz  -- numpy only. `emb` and `label` are plain arrays and everything else
             is a JSON string, so there is no pickle in the load path either.
             torch.load(weights_only=False) on the old format deserializes
             whatever the file says, and a gallery arrives over the network
             from a retrain.
  * .pt   -- the previous format, read through torch. Kept so a host can read
             the gallery it already has, and so a rollback has something to
             land on. Writing this format is gone.

Read support ships before anything writes .npz: production has to be able to
read the new format before a retrain starts producing it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import numpy as np

#Bumped only for a change that an older reader could not make sense of.
FORMAT_VERSION = 1

NPZ_SUFFIX = ".npz"

#Keys carried outside the two arrays, as one JSON blob.
_META_KEYS = ("class_to_idx", "idx_to_class", "path", "records", "record_schema")


class GalleryFormatError(ValueError):
    """The file is not a gallery this code can read."""


def is_npz(path: Any) -> bool:
    return str(path or "").lower().endswith(NPZ_SUFFIX)


def _coerce_embeddings(raw: Any) -> np.ndarray:
    """Embeddings as a contiguous float32 (n, d) array.

    Accepts a torch tensor too, so the .pt path and a retrain that still has
    tensors in hand both land here.
    """
    if hasattr(raw, "detach"):  #a torch tensor
        raw = raw.detach().cpu().numpy()
    emb = np.ascontiguousarray(np.asarray(raw, dtype=np.float32))
    if emb.ndim != 2 or emb.shape[0] == 0 or emb.shape[1] == 0:
        raise GalleryFormatError(f"embeddings must be a non-empty 2-D array, got {emb.shape}")
    return emb


def _coerce_labels(raw: Any) -> np.ndarray:
    if hasattr(raw, "detach"):
        raw = raw.detach().cpu().numpy()
    return np.ascontiguousarray(np.asarray(raw, dtype=np.int64).reshape(-1))


def read_gallery(path: Any) -> Dict[str, Any]:
    """Read a gallery from either format.

    Returns a plain dict: `emb` (float32 ndarray), `label` (int64 ndarray),
    and whichever of the metadata keys the file carried. Raises
    GalleryFormatError if the file is not readable as a gallery.
    """
    target = str(path)
    if is_npz(target):
        return _load_npz(target)
    return _load_pt(target)


def _load_npz(target: str) -> Dict[str, Any]:
    #allow_pickle stays off: the arrays are plain and the rest is JSON.
    with np.load(target, allow_pickle=False) as data:
        keys = set(data.files)
        if "emb" not in keys:
            raise GalleryFormatError(f"{target}: no 'emb' array")
        out: Dict[str, Any] = {
            "emb": _coerce_embeddings(data["emb"]),
            "label": _coerce_labels(data["label"]) if "label" in keys else np.zeros(0, np.int64),
        }
        meta: Dict[str, Any] = {}
        if "meta" in keys:
            try:
                meta = json.loads(str(data["meta"].item()))
            except Exception as exc:
                raise GalleryFormatError(f"{target}: unreadable meta ({exc})") from exc
    #class_to_idx round-trips through JSON, which makes every key a string.
    if isinstance(meta.get("idx_to_class"), dict):
        meta["idx_to_class"] = {int(k): v for k, v in meta["idx_to_class"].items()}
    out.update({k: v for k, v in meta.items() if k in _META_KEYS})
    return out


def _load_pt(target: str) -> Dict[str, Any]:
    """Read the previous torch format. Only reached for a .pt on disk."""
    import torch  #deferred: the whole point is not to pay for this on .npz

    try:
        raw = torch.load(target, map_location="cpu", weights_only=True)
    except Exception:
        #Older galleries carry python objects in `records`, which weights_only
        #refuses. This is why .npz exists.
        raw = torch.load(target, map_location="cpu", weights_only=False)
    if not isinstance(raw, dict):
        raise GalleryFormatError(f"{target}: expected a dict, got {type(raw).__name__}")
    if "emb" not in raw and "embeddings" not in raw:
        raise GalleryFormatError(f"{target}: no embeddings")
    out: Dict[str, Any] = {
        "emb": _coerce_embeddings(raw.get("emb") if "emb" in raw else raw.get("embeddings")),
    }
    labels = raw.get("label") if "label" in raw else raw.get("labels")
    out["label"] = _coerce_labels(labels) if labels is not None else np.zeros(0, np.int64)
    for key in _META_KEYS:
        if key in raw:
            out[key] = raw[key]
    #The path list has gone by three names over the gallery's life.
    if "path" not in out:
        for alias in ("paths", "img_paths"):
            if alias in raw:
                out["path"] = raw[alias]
                break
    if "records" not in out and "gallery_records" in raw:
        out["records"] = raw["gallery_records"]
    return out


def save_npz(path: Any, data: Dict[str, Any]) -> str:
    """Write a gallery as .npz. Returns the path written.

    Writes to a temporary file and renames, so a reader never sees a partial
    gallery -- a half-written one would take the bot's identify down until the
    next retrain.
    """
    target = Path(str(path))
    if target.suffix.lower() != NPZ_SUFFIX:
        target = target.with_suffix(NPZ_SUFFIX)
    target.parent.mkdir(parents=True, exist_ok=True)

    emb = _coerce_embeddings(data.get("emb") if "emb" in data else data.get("embeddings"))
    labels = data.get("label") if "label" in data else data.get("labels")
    label = _coerce_labels(labels) if labels is not None else np.zeros(emb.shape[0], np.int64)

    meta: Dict[str, Any] = {"format_version": FORMAT_VERSION}
    for key in _META_KEYS:
        if key in data and data[key] is not None:
            meta[key] = data[key]
    #Keys have to be strings to survive JSON; the reader turns them back.
    if isinstance(meta.get("idx_to_class"), dict):
        meta["idx_to_class"] = {str(k): v for k, v in meta["idx_to_class"].items()}

    tmp = target.with_name(target.name + ".tmp.npz")
    np.savez_compressed(tmp, emb=emb, label=label, meta=np.array(json.dumps(meta)))
    tmp.replace(target)
    return str(target)


def embedding_count(path: Any) -> int:
    """How many embeddings a gallery holds, without loading the whole thing.

    Used to sanity-check a retrain against the gallery it replaces.
    """
    target = str(path)
    if is_npz(target):
        with np.load(target, allow_pickle=False) as data:
            if "emb" not in data.files:
                return 0
            return int(data["emb"].shape[0])
    return int(read_gallery(target)["emb"].shape[0])
