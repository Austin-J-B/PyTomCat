"""Convert a DINOv3 gallery from the torch .pt format to .npz.

The gallery is the only model artifact the host process needs when
CV_BACKEND=modal, and reading a .pt costs an `import torch`: ~358MB resident
and 2.5s, on a 4GB box that OOM-kills. Nothing in the file needs torch.

This reads the .pt (which does need torch, once, here rather than in the bot)
and writes the same content as .npz next to it, then checks the two agree
exactly -- embeddings bit for bit, labels, names and per-row metadata.

    python scripts/convert_gallery_to_npz.py                     # the configured gallery
    python scripts/convert_gallery_to_npz.py weights/R6_*.pt     # a specific one
    python scripts/convert_gallery_to_npz.py --all               # every .pt in weights/

The .pt is left in place. Nothing here deletes a gallery: a rollback needs it,
and the reader still understands both formats.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from tomcat.vision import gallery_file


def convert(source: Path) -> int:
    print(f"\n{source}")
    if not source.exists() or source.stat().st_size == 0:
        print("  SKIP missing or empty")
        return 1

    original = gallery_file.read_gallery(str(source))
    rows = int(original["emb"].shape[0])
    print(f"  read   {rows} x {int(original['emb'].shape[1])} embeddings, "
          f"{len(set(original.get('label', []).tolist()))} labels, "
          f"{source.stat().st_size / 1e6:.1f} MB")

    target = gallery_file.save_npz(source.with_suffix(gallery_file.NPZ_SUFFIX), original)
    size_mb = Path(target).stat().st_size / 1e6
    print(f"  wrote  {target}  {size_mb:.1f} MB")

    #Read it back and insist it is the same gallery. A silent mismatch here
    #would move every similarity score in the bot.
    check = gallery_file.read_gallery(target)
    problems: list[str] = []
    if not np.array_equal(check["emb"], original["emb"]):
        worst = float(np.abs(check["emb"] - original["emb"]).max())
        problems.append(f"embeddings differ (max {worst:.3e})")
    if not np.array_equal(check["label"], original["label"]):
        problems.append("labels differ")
    for key in ("class_to_idx", "path", "records", "record_schema"):
        if key in original and check.get(key) != original.get(key):
            problems.append(f"{key} differs")
    #The names each row resolves to are what the bot actually shows.
    def names(data):
        mapping = data.get("idx_to_class") or {
            v: k for k, v in (data.get("class_to_idx") or {}).items()
        }
        return [mapping.get(int(i)) for i in data["label"]]
    if names(check) != names(original):
        problems.append("resolved cat names differ")

    if problems:
        print("  FAIL   " + "; ".join(problems))
        return 1
    print(f"  verified: identical embeddings, labels and metadata")
    return 0


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("-")]
    weights = Path(__file__).resolve().parents[1] / "weights"

    if "--all" in argv:
        sources = sorted(weights.glob("R*_cat_DINOv3_gallery*.pt"))
        if not sources:
            print(f"No .pt galleries in {weights}")
            return 1
    elif args:
        sources = [Path(a) for a in args]
    else:
        from tomcat.config import settings
        configured = str(getattr(settings, "cv_gallery_path", "") or "").strip()
        if not configured:
            print("No gallery configured; pass a path or --all")
            return 1
        sources = [Path(configured)]

    print("=" * 70)
    print("gallery .pt -> .npz")
    print("=" * 70)
    failures = sum(convert(s) for s in sources if s.suffix.lower() != ".npz")
    print("\n" + "=" * 70)
    if failures:
        print(f"{failures} file(s) failed.")
        return 1
    print("Done. The .pt files are left in place; the reader understands both.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
