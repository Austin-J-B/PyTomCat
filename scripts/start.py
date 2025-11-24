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
    # Use your actual tunnel name here:

    # Use bundled cloudflared.exe from project root (Windows)
    if os.name == "nt":
        cloudflared_path = str(ROOT / "cloudflared.exe")
    else:
        cloudflared_path = "cloudflared"
    cloudflared_cmd = [cloudflared_path, "tunnel", "run", "tomcat-ui"]

    # Start API server
    api_proc = subprocess.Popen(cmd, cwd=ROOT)
    # Start cloudflared tunnel
    tunnel_proc = subprocess.Popen(cloudflared_cmd, cwd=ROOT)

    print("TomCat API server and Cloudflare tunnel are running. Press Ctrl+C to stop both.")
    try:
        api_proc.wait()
        tunnel_proc.wait()
    except KeyboardInterrupt:
        print("\nShutting down...")
        api_proc.terminate()
        tunnel_proc.terminate()


if __name__ == "__main__":
    main()
