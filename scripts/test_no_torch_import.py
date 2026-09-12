"""The web server process must not import torch.

With CV_BACKEND=modal, detection and embedding run on Modal. The host loads the
gallery and scores queries against it -- an L2 normalize, a matmul and a per-cat
max -- which is numpy's job. Importing torch to do that cost 358MB resident and
2.5s of startup on a 4GB box that OOM-kills:

    before                                  after
    import tomcat.vision.vision  489 MB     60 MB
    import tomcat.handlers.labeler 518 MB   93 MB

That property is one stray module-level import away from silently coming back,
and nothing else would fail when it did -- the bot would just be 400MB heavier.
So it is asserted here. Importing torch is fine inside the functions that run a
model; what must not happen is paying for it to import a module.

If this fails, find the culprit with:

    python -c "import sys,builtins; r=builtins.__import__; \
      builtins.__import__=lambda n,*a,**k: (print(n) if n=='torch' else 0) or r(n,*a,**k); \
      __import__('tomcat.handlers.labeler')"

Run: python scripts/test_no_torch_import.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import List

ROOT = Path(__file__).resolve().parents[1]

#Each of these has pulled torch in at some point: vision through ultralytics,
#labeler through gallery_retrain -> gallery_updater, backend through a
#`from torch import Tensor` used only in annotations.
MODULES = [
    "tomcat.vision.gallery_file",
    "tomcat.vision.backend",
    "tomcat.vision.vision",
    "tomcat.services.gallery_updater",
    "tomcat.handlers.labeler",
]

FAILURES: List[str] = []

#Imported in a subprocess so one module's imports cannot mask another's, and so
#a torch already in sys.modules from the test runner cannot hide a regression.
#
#Deliberately no _test_support here: it probes each optional dependency with
#__import__ to decide what to stub, which imports torch on any machine that
#has it and would make this check vacuous. Nothing is stubbed at all, because
#stubbing ultralytics -- the module that used to drag torch in -- would hide
#exactly the regression this is meant to catch.
PROBE = r"""
import sys
sys.path.insert(0, {root!r})
import importlib
importlib.import_module({module!r})
loaded = sorted(m for m in sys.modules if m == "torch" or m.startswith("torch."))
print("TORCH" if loaded else "CLEAN")
"""


def main() -> int:
    print("=" * 70)
    print("no-torch-on-import check")
    print("=" * 70)

    try:
        import torch  # noqa: F401
        print("\n(real torch is installed here, so a regression would be caught)")
    except Exception:
        #Without torch installed, a stray module-level import raises
        #ModuleNotFoundError instead, which this reports as a failed import.
        print("\n(torch is not installed here; a stray import fails outright)")

    for module in MODULES:
        code = PROBE.format(root=str(ROOT), module=module)
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        out = (proc.stdout or "").strip().splitlines()
        verdict = out[-1] if out else ""
        if proc.returncode != 0:
            FAILURES.append(module)
            print(f"  FAIL {module} did not import")
            print("       " + (proc.stderr or "").strip().splitlines()[-1][:160])
        elif verdict == "CLEAN":
            print(f"  PASS {module}")
        else:
            FAILURES.append(module)
            print(f"  FAIL {module} imported torch")

    print("\n" + "=" * 70)
    if FAILURES:
        print(f"{len(FAILURES)} module(s) import torch: {', '.join(FAILURES)}")
        print("Move the import inside the function that runs a model, or put it")
        print("under TYPE_CHECKING if it is only used in an annotation.")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
