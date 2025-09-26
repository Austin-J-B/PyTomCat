#!/usr/bin/env python3
"""Launch TomCat using the project virtual environment."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
VENV_DIR = ROOT / ".venv"


def _venv_python() -> Path:
    if os.name == "nt":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def main() -> None:
    python_path = _venv_python()
    if not python_path.exists():
        print("Virtual environment not found. Run `python scripts/install.py` first.")
        sys.exit(1)

    cmd = [str(python_path), "-m", "tomcat.main"]
    print(f"Starting TomCat (cwd={ROOT})\n→ {' '.join(cmd)}")
    subprocess.run(cmd, cwd=ROOT)


if __name__ == "__main__":
    main()
