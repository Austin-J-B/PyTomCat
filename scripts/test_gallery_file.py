"""Gallery file format: .npz and .pt must describe the same gallery.

The gallery is the only model artifact the host needs when CV_BACKEND=modal,
and reading the old .pt format costs an `import torch` -- ~358MB resident and
2.5s, on a 4GB box that OOM-kills. Nothing in the file needs torch, so .npz is
the format now and both are readable.

Getting this wrong is quiet and expensive: every similarity score in the bot is
computed against these embeddings, so a row that loads in the wrong order, a
label that resolves to the wrong cat, or a float that shifts would change what
the bot says a cat is without anything failing.

Run: python scripts/test_gallery_file.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

#Sends this process's machine log to a scratch directory, so the test does
#not write records into the corpus the real logs are analysed from.
import _test_support  # noqa: F401

import numpy as np

from tomcat.vision import gallery_file

#Reading the old .pt format needs torch, which CI does not install. That is the
#whole reason .npz exists, so the tests say so rather than requiring it.
#
#Ask _test_support rather than trying the import: it stands a MagicMock in for
#a missing torch, and a MagicMock answers every call, so `import torch`
#succeeding proves nothing and the tensor checks below would run against it.
_HAVE_TORCH = "torch" not in _test_support.STUBBED

FAILURES: List[str] = []


def check(label: str, expected, actual) -> None:
    if expected == actual:
        print(f"  PASS {label}")
    else:
        FAILURES.append(label)
        print(f"  FAIL {label}\n       expected {expected!r}\n       got      {actual!r}")


def ok(label: str, condition: bool, detail: str = "") -> None:
    check(label, True, bool(condition)) if condition else check(
        label + (f" ({detail})" if detail else ""), True, False)


def sample(rows: int = 7, dim: int = 4) -> dict:
    rng = np.random.default_rng(11)
    return {
        "emb": rng.standard_normal((rows, dim), dtype=np.float32),
        "label": np.arange(rows, dtype=np.int64) % 3,
        "class_to_idx": {"Eraser": 0, "Microwave": 1, "Twix": 2},
        "path": [f"crop://sn{i}_c01" for i in range(rows)],
        "records": [{"serial": i, "crop": 1, "cat_name": "Eraser"} for i in range(rows)],
        "record_schema": "sn_crop_v1",
    }


def main() -> int:
    print("=" * 70)
    print("gallery file format tests")
    print("=" * 70)
    tmp = Path(tempfile.mkdtemp(prefix="galleryfmt"))

    print("\n[1] a gallery survives the round trip exactly")
    original = sample()
    written = gallery_file.save_npz(tmp / "R9_cat_DINOv3_gallery.npz", original)
    back = gallery_file.read_gallery(written)
    ok("embeddings are bit-identical", np.array_equal(back["emb"], original["emb"]))
    ok("labels are bit-identical", np.array_equal(back["label"], original["label"]))
    check("class_to_idx", original["class_to_idx"], back["class_to_idx"])
    check("per-row paths", original["path"], back["path"])
    check("per-row records", original["records"], back["records"])
    check("record schema", original["record_schema"], back["record_schema"])
    check("embeddings stay float32", "float32", str(back["emb"].dtype))
    check("labels stay int64", "int64", str(back["label"].dtype))

    print("\n[2] the cat each row resolves to")
    #This is what the bot actually shows, so it is worth asserting directly
    #rather than trusting class_to_idx alone.
    idx_to_class = {v: k for k, v in back["class_to_idx"].items()}
    check("names in row order",
          ["Eraser", "Microwave", "Twix", "Eraser", "Microwave", "Twix", "Eraser"],
          [idx_to_class[int(i)] for i in back["label"]])

    print("\n[3] the suffix is taken care of")
    written = gallery_file.save_npz(tmp / "named_without_suffix", sample())
    check("a missing suffix is added", True, written.endswith(".npz"))
    ok("and it reads back", gallery_file.read_gallery(written)["emb"].shape == (7, 4))

    print("\n[4] a partial write is never visible")
    #A half-written gallery would take identify down until the next retrain.
    target = tmp / "R9_atomic.npz"
    gallery_file.save_npz(target, sample())
    leftovers = sorted(p.name for p in tmp.iterdir() if ".tmp" in p.name)
    check("no temporary file left behind", [], leftovers)

    print("\n[5] the row count is read without loading the gallery")
    check("count matches", 7, gallery_file.embedding_count(target))

    print("\n[6] a file that is not a gallery is rejected, not guessed at")
    broken = tmp / "broken.npz"
    np.savez_compressed(broken, something_else=np.zeros(3))
    try:
        gallery_file.read_gallery(broken)
        check("no 'emb' array raises", True, False)
    except gallery_file.GalleryFormatError:
        check("no 'emb' array raises", True, True)

    empty = tmp / "empty.npz"
    np.savez_compressed(empty, emb=np.zeros((0, 4), np.float32),
                        meta=np.array(json.dumps({})))
    try:
        gallery_file.read_gallery(empty)
        check("an empty gallery raises", True, False)
    except gallery_file.GalleryFormatError:
        check("an empty gallery raises", True, True)

    print("\n[7] torch tensors are accepted on the way in")
    #A retrain has tensors in hand; it should not have to convert them itself.
    if not _HAVE_TORCH:
        print("  SKIP torch not installed")
    else:
        import torch

        data = sample()
        data["emb"] = torch.from_numpy(data["emb"])
        data["label"] = torch.from_numpy(data["label"])
        written = gallery_file.save_npz(tmp / "R9_from_tensors.npz", data)
        back = gallery_file.read_gallery(written)
        ok("embeddings match the tensor",
           np.array_equal(back["emb"], sample()["emb"]))
        ok("labels match the tensor",
           np.array_equal(back["label"], sample()["label"]))

    print("\n[8] the real gallery, if it is here")
    from tomcat.config import settings

    def readable(path: Path) -> bool:
        """Whether this file can be read here. Reading a .pt needs torch."""
        if not path.is_file():
            return False
        if path.suffix.lower() == gallery_file.NPZ_SUFFIX:
            return True
        return bool(_HAVE_TORCH)

    configured = Path(str(getattr(settings, "cv_gallery_path", "") or ""))
    if configured.is_file() and not readable(configured):
        #CI installs requirements-test.txt, which has no torch, and the
        #repository still carries a .pt gallery. That combination is expected,
        #and being unable to read the old format without torch is the point.
        print(f"  SKIP {configured.name} is the old format and torch is not installed")
    elif readable(configured):
        real = gallery_file.read_gallery(str(configured))
        rows, dim = real["emb"].shape
        ok("it has embeddings", rows > 0 and dim > 0, f"{rows}x{dim}")
        check("one label per row", rows, int(real["label"].shape[0]))
        check("one path per row", rows, len(real.get("path") or []))
        names = real.get("idx_to_class") or {
            v: k for k, v in (real.get("class_to_idx") or {}).items()
        }
        ok("every label resolves to a cat",
           all(int(i) in names for i in real["label"]))
        #Both formats on disk have to agree, since either may be read: the bot
        #prefers the .npz and a rolled-back host would find the .pt.
        twin = configured.with_suffix(".pt" if configured.suffix == ".npz" else ".npz")
        if readable(twin):
            other = gallery_file.read_gallery(str(twin))
            ok("the .pt and .npz on disk are the same gallery",
               np.array_equal(other["emb"], real["emb"])
               and np.array_equal(other["label"], real["label"]))
        elif twin.is_file():
            print(f"  SKIP {twin.name} needs torch to read")
        else:
            print(f"  SKIP no counterpart for {configured.name}")
    else:
        print(f"  SKIP no gallery at {configured}")

    print("\n[9] discovery prefers .npz at the same version, and version wins first")
    from tomcat import config
    keyed = [
        ("R5.5.4_cat_DINOv3_gallery.pt", ((5, 5, 4), 0)),
        ("R5.5.4_cat_DINOv3_gallery.npz", ((5, 5, 4), 1)),
        ("R6_cat_DINOv3_gallery.pt", ((6,), 0)),
        ("not_a_gallery.pt", None),
    ]
    for name, expected in keyed:
        check(name, expected, config._gallery_version_key(Path(name)))
    ok("R6.pt still beats R5.5.4.npz",
       config._gallery_version_key(Path("R6_cat_DINOv3_gallery.pt"))
       > config._gallery_version_key(Path("R5.5.4_cat_DINOv3_gallery.npz")))
    ok("R6.npz beats R6.pt",
       config._gallery_version_key(Path("R6_cat_DINOv3_gallery.npz"))
       > config._gallery_version_key(Path("R6_cat_DINOv3_gallery.pt")))

    print("\n" + "=" * 70)
    if FAILURES:
        print(f"{len(FAILURES)} failure(s).")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
