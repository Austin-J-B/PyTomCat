"""TomCatGPT worker: keeps the language model loaded and answers one JSON request per line.

Started by the bot (tomcat/handlers/tomcatgpt.py) as its own process, so the bot never imports torch and an
out-of-memory kill takes this worker, not the bot (it raises its own OOM score at start).

    python -m tomcat.lm.worker BUNDLE_DIR [--threads N]

stdout, one JSON object per line:
    {"ready": true, "load_seconds": s, "step": n}                once, after loading
    {"id": i, "text": "...", "candidates": n, "seconds": s}      per request
    {"id": i, "error": "..."}                                    when a request fails
stdin, one JSON object per line: {"id": i, "messages": [{"s": 1, "text": "..."}, ...]}
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from pathlib import Path

try:
    import resource
except ImportError:  # Windows (local runs)
    resource = None


def raise_oom_score() -> None:
    try:  # Linux only; a process may always make itself likelier to be killed
        Path("/proc/self/oom_score_adj").write_text("1000")
    except OSError:
        pass


def peak_mb() -> int | None:
    """Peak resident memory of this process so far (Linux reports KB); None where unavailable."""
    if resource is None:
        return None
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("bundle", type=Path)
    ap.add_argument("--threads", type=int, default=2)
    args = ap.parse_args()
    raise_oom_score()
    out = sys.stdout
    started = time.perf_counter()
    try:
        from .engine import Engine
        engine = Engine(args.bundle, threads=args.threads)
    except Exception as exc:
        out.write(json.dumps({"ready": False, "error": f"{type(exc).__name__}: {exc}"}) + "\n")
        out.flush()
        traceback.print_exc()
        sys.exit(1)
    out.write(json.dumps({"ready": True, "load_seconds": round(time.perf_counter() - started, 2),
                          "step": engine.step, "peak_mb": peak_mb()}) + "\n")
    out.flush()
    for line in sys.stdin:
        if not line.strip():
            continue
        rid = None
        try:
            request = json.loads(line)
            rid = request.get("id")
            t0 = time.perf_counter()
            result = engine.reply(request["messages"])
            response = {"id": rid, "text": result["text"], "candidates": result["candidates"],
                        "seconds": round(time.perf_counter() - t0, 2), "peak_mb": peak_mb()}
        except Exception as exc:
            traceback.print_exc()
            response = {"id": rid, "error": f"{type(exc).__name__}: {exc}"}
        out.write(json.dumps(response, ensure_ascii=False) + "\n")
        out.flush()


if __name__ == "__main__":
    main()
