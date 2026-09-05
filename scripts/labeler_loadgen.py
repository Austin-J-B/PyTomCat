#!/usr/bin/env python3
"""Drive the labeler's image paths hard enough to reproduce its memory growth.

Sessions climb ~2GB of resident memory in about a minute of classify work while
every cache stays small, and diagnosing that has meant asking a volunteer to go
label for a while and then reading logs afterwards. This runs the same code in a
throwaway process instead, samples the heap census on the way, and prints what is
holding the decoded images.

Only local paths run by default. Detect and identify go out to a Modal GPU that
bills for the time, and they are not where the memory goes, so --modal is opt-in.

    scripts/labeler_loadgen.py --seconds 90
    scripts/labeler_loadgen.py --seconds 300 --workers 12 --sample-every 10

Run it under a memory cap so a runaway cannot disturb a bot on the same host:

    ( ulimit -v 3000000; .venv/bin/python scripts/labeler_loadgen.py )
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def rss_mb() -> float:
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0
    except Exception:
        pass
    return -1.0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--seconds", type=int, default=90, help="how long to apply load (default 90)")
    p.add_argument("--workers", type=int, default=7,
                   help="concurrent render threads; 7 matches asyncio.to_thread's default pool here")
    p.add_argument("--sample-every", type=float, default=5.0, help="seconds between heap samples")
    p.add_argument("--serials", type=int, default=400, help="how many distinct photos to cycle through")
    p.add_argument("--modal", action="store_true",
                   help="also drive detect/identify, which bill for GPU time (default off)")
    p.add_argument("--json", type=str, default="", help="write the samples to this path as JSON")
    return p.parse_args()


def pick_boxes(rng: random.Random) -> Tuple[float, float, float, float]:
    """A plausible cat box: mostly small-in-frame, occasionally filling it."""
    if rng.random() < 0.15:
        w = rng.uniform(0.6, 0.95)
    else:
        w = rng.uniform(0.08, 0.35)
    h = min(0.95, w * rng.uniform(0.8, 1.4))
    cx = rng.uniform(w / 2, 1 - w / 2)
    cy = rng.uniform(h / 2, 1 - h / 2)
    return (cx, cy, w, h)


def main() -> int:
    args = parse_args()
    os.environ.setdefault("LABELER_MEMORY_LOG_FLOOR_MB", "400")
    os.environ.setdefault("LABELER_MEMORY_LOG_STEP_MB", "100")
    os.environ.setdefault("LABELER_PIL_HOLDER_MIN_TOTAL_MB", "100")

    print("importing labeler (loads models; takes a moment)...", flush=True)
    from tomcat.handlers import labeler
    from tomcat.services import local_photos
    from tomcat.vision import vision as V

    captured: List[Tuple[Any, ...]] = []
    labeler.log_action = lambda *a, **k: captured.append(a)

    serials = sorted(local_photos.local_serials())
    if not serials:
        print("no local photos found; nothing to drive", file=sys.stderr)
        return 1
    rng = random.Random(17)
    pool = rng.sample(serials, min(args.serials, len(serials)))
    print(f"{len(serials)} local photos, cycling {len(pool)}; "
          f"{args.workers} workers for {args.seconds}s"
          f"{' (+modal detect/identify)' if args.modal else ''}\n", flush=True)

    stop = threading.Event()
    counts = {"ref_crop": 0, "ref_entry": 0, "detect": 0, "identify": 0, "errors": 0}
    counts_lock = threading.Lock()

    def bump(key: str) -> None:
        with counts_lock:
            counts[key] += 1

    def worker(seed: int) -> None:
        r = random.Random(seed)
        while not stop.is_set():
            sn = r.choice(pool)
            try:
                data = local_photos.read_local_photo_bytes(int(sn))
                if not data:
                    continue
                #The classify view's hot path: one decode per reference crop.
                labeler._render_ref_crop_jpeg(data, pick_boxes(r), 320, 0.12)
                bump("ref_crop")

                #The ref-cache path, which returns live PIL crops to its caller.
                if r.random() < 0.25:
                    box = pick_boxes(r)
                    coord = f"{box[0]:.6f} {box[1]:.6f} {box[2]:.6f} {box[3]:.6f}"
                    crops, _refs = V._collect_labeler_ref_entries(
                        [(int(sn), coord, 1)], thumb_size=96
                    )
                    for c in crops:
                        try:
                            c.close()
                        except Exception:
                            pass
                    bump("ref_entry")

                if args.modal and r.random() < 0.08:
                    V.detect(data, include_boxed_image=False)
                    bump("detect")
            except Exception:
                bump("errors")

    samples: List[Dict[str, Any]] = []

    def sample(tag: str) -> None:
        captured.clear()
        labeler._memory_watermark_mb = 0
        t0 = time.perf_counter()
        try:
            labeler._check_memory_watermark()
        except Exception as e:
            print(f"  census failed: {type(e).__name__}: {e!r}", flush=True)
            return
        took = (time.perf_counter() - t0) * 1000
        rows = [c for c in captured if c and c[0] == "labeler_memory_watermark"]
        if not rows:
            print(f"{tag}  rss={rss_mb():.0f}MB  (below census floor)", flush=True)
            return
        body = json.loads(rows[0][2])
        heap, mem = body.get("heap", {}), body.get("memory", {})
        with counts_lock:
            done = dict(counts)
        rec = {"tag": tag, "rss_mb": mem.get("rss_mb"), "heap": heap, "work": done,
               "census_ms": int(took)}
        samples.append(rec)
        print(f"{tag}  rss={mem.get('rss_mb')}MB  pil={heap.get('pil_mb')}MB/{heap.get('pil_count')}"
              f"  torch={heap.get('torch_tensor_mb')}MB  numpy={heap.get('numpy_mb')}MB"
              f"  [crops={done['ref_crop']} entries={done['ref_entry']} err={done['errors']}]"
              f"  census={int(took)}ms walk={heap.get('pil_holders_ms')}ms", flush=True)
        for row in heap.get("pil_holders") or []:
            print(f"        {row.get('images'):5d} images  <-  {row.get('holder')}", flush=True)

    sample("baseline ")
    threads = [threading.Thread(target=worker, args=(100 + i,), daemon=True)
               for i in range(args.workers)]
    for t in threads:
        t.start()

    deadline = time.time() + args.seconds
    next_sample = time.time() + args.sample_every
    try:
        while time.time() < deadline:
            time.sleep(0.25)
            if time.time() >= next_sample:
                sample(f"t+{int(args.seconds - (deadline - time.time())):>3}s ")
                next_sample = time.time() + args.sample_every
    except KeyboardInterrupt:
        print("\ninterrupted", flush=True)
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=10)

    print()
    sample("final    ")
    #What survives once the workers are gone is retention rather than work in flight.
    import gc
    gc.collect()
    sample("post-gc  ")

    if args.json:
        Path(args.json).write_text(json.dumps(samples, indent=2), encoding="utf-8")
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
