"""Opening a photo must produce the same pixels, for less work.

_open_rgb_image is the single entry point for every photo the CV code touches --
identify, detect, crop refinement, quality scoring. It used to call
ImageOps.exif_transpose and .convert("RGB") unconditionally, and both return a
new image even when they change nothing. The club's photos are 12 megapixels
and already RGB with no rotation, so that was 36MB copied twice per decode, for
nothing. Measured over ten real photos: 91.5ms -> 63.2ms.

Skipping work is only safe if the result is identical, so that is what this
asserts -- pixel for pixel, including the rotated case that the local photo
library happens not to contain.

Run: python scripts/test_image_decode.py
"""

from __future__ import annotations

import io
import sys
from pathlib import Path
from typing import List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _test_support  # noqa: F401

from PIL import Image, ImageOps

from tomcat.vision import vision as V

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


def reference(data: bytes) -> Image.Image:
    """What _open_rgb_image did before: transpose and convert, always."""
    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img)
    return img.convert("RGB")


def gradient(w: int = 64, h: int = 48) -> Image.Image:
    img = Image.new("RGB", (w, h))
    px = img.load()
    for y in range(h):
        for x in range(w):
            px[x, y] = (x * 3 % 256, y * 5 % 256, (x + y) % 256)
    return img


def encode(img: Image.Image, fmt: str = "JPEG", **kw) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format=fmt, **kw)
    return buf.getvalue()


def same_pixels(a: Image.Image, b: Image.Image) -> bool:
    return a.size == b.size and a.mode == b.mode and a.tobytes() == b.tobytes()


def main() -> int:
    print("=" * 70)
    print("image decode tests")
    print("=" * 70)

    print("\n[1] an ordinary RGB photo with no rotation")
    data = encode(gradient(), quality=95)
    got = V._open_rgb_image(io.BytesIO(data))
    check("mode", "RGB", got.mode)
    ok("identical to transpose-and-convert", same_pixels(got, reference(data)))
    ok("the pixels are actually loaded", got.im is not None)

    print("\n[2] a rotated photo still gets rotated")
    #The local library has no EXIF-rotated photos, so one is built here: without
    #it, skipping exif_transpose would look correct and silently serve every
    #phone photo sideways.
    upright = gradient(80, 40)
    exif = Image.Exif()
    exif[V._EXIF_ORIENTATION_TAG] = 6  #rotate 270 on load
    data = encode(upright, quality=95, exif=exif)

    with Image.open(io.BytesIO(data)) as probe:
        ok("the fixture really carries an orientation tag",
           V._has_exif_orientation(probe))

    got = V._open_rgb_image(io.BytesIO(data))
    want = reference(data)
    check("the rotation is applied, so width and height swap", want.size, got.size)
    ok("identical to transpose-and-convert", same_pixels(got, want))
    ok("and it is not the unrotated image", got.size != upright.size,
       f"{got.size} == {upright.size}")

    print("\n[3] every orientation value behaves as before")
    for value in (1, 2, 3, 4, 5, 6, 7, 8):
        exif = Image.Exif()
        exif[V._EXIF_ORIENTATION_TAG] = value
        data = encode(gradient(72, 36), quality=95, exif=exif)
        ok(f"orientation {value}", same_pixels(V._open_rgb_image(io.BytesIO(data)),
                                               reference(data)))

    print("\n[4] images that are not already RGB are converted")
    for mode, fmt in (("L", "PNG"), ("RGBA", "PNG"), ("P", "PNG")):
        src = gradient(40, 30).convert(mode)
        data = encode(src, fmt=fmt)
        got = V._open_rgb_image(io.BytesIO(data))
        check(f"{mode} becomes RGB", "RGB", got.mode)
        ok(f"{mode} matches the old path", same_pixels(got, reference(data)))

    print("\n[5] a missing or broken orientation tag is treated as upright")
    plain = Image.open(io.BytesIO(encode(gradient())))
    ok("no exif at all", not V._has_exif_orientation(plain))
    for value in (0, 1, 99, "banana", None):
        exif = Image.Exif()
        try:
            exif[V._EXIF_ORIENTATION_TAG] = value
            data = encode(gradient(48, 24), quality=95, exif=exif)
        except Exception:
            continue
        with Image.open(io.BytesIO(data)) as probe:
            rotated = V._has_exif_orientation(probe)
        ok(f"orientation {value!r} needs no transpose", not rotated)

    print("\n[6] the real photo library, if it is here")
    from tomcat.services import local_photos
    root = local_photos.photo_root()
    photos = []
    if root.exists():
        for path in sorted(root.rglob("*.jpg"))[:200]:
            try:
                if path.stat().st_size > 100_000:
                    photos.append(path)
            except Exception:
                continue
            if len(photos) >= 5:
                break
    if not photos:
        print(f"  SKIP no photos under {root}")
    else:
        mismatched = []
        for path in photos:
            raw = path.read_bytes()
            if not same_pixels(V._open_rgb_image(io.BytesIO(raw)), reference(raw)):
                mismatched.append(path.name)
        ok(f"{len(photos)} real photos decode identically", not mismatched,
           ", ".join(mismatched))

    print("\n[7] the identify profiler records a fair sample, not just the slow tail")
    #A median over "calls that took five seconds or more" cannot show whether
    #anything improved.
    real_every, real_slow = V._PROFILE_SAMPLE_EVERY, V._PROFILE_SLOW_MS
    try:
        V._PROFILE_SAMPLE_EVERY = 10
        V._PROFILE_SLOW_MS = 5000.0
        V._profile_counter = 0
        reasons = [V._profile_reason(100.0, 10.0, None) for _ in range(30)]
        check("one in ten fast calls is sampled", 3, sum(1 for r in reasons if r == "sampled"))
        check("and the rest are skipped", 27, sum(1 for r in reasons if not r))

        check("a slow call is always logged", "slow", V._profile_reason(9000.0, 10.0, None))
        check("slow gallery refs too", "slow_refs", V._profile_reason(100.0, 9000.0, None))
        check("an explicitly traced call", "traced", V._profile_reason(1.0, 1.0, "rid=7"))

        #A slow call must not consume the sampling counter, or the sample rate
        #would drift with how bad the day was.
        V._profile_counter = 0
        for _ in range(5):
            V._profile_reason(9000.0, 0.0, None)
        check("slow calls leave the sample counter alone", 0, V._profile_counter)

        V._PROFILE_SAMPLE_EVERY = 0
        V._profile_counter = 0
        check("sampling can be turned off", "", V._profile_reason(100.0, 10.0, None))
        check("leaving slow-only, as before", "slow", V._profile_reason(9000.0, 10.0, None))

        V._PROFILE_SAMPLE_EVERY = 1
        V._profile_counter = 0
        check("or turned up to everything", "sampled", V._profile_reason(1.0, 1.0, None))
    finally:
        V._PROFILE_SAMPLE_EVERY, V._PROFILE_SLOW_MS = real_every, real_slow

    print("\n" + "=" * 70)
    if FAILURES:
        print(f"{len(FAILURES)} failure(s).")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
