#!/usr/bin/env python3
"""Launch TomCat using the project virtual environment.

Starts:
  1. The Discord Bot (API Server)
  2. The Cloudflare Tunnel (tomcat-ui)
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
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
        print("❌ Virtual environment not found. Run `python scripts/install.py` first.")
        sys.exit(1)

    # 1. Prepare Bot Command
    bot_cmd = [str(python_path), "-m", "tomcat.main"]

    # 2. Prepare Cloudflare Tunnel Command
    if os.name == "nt":
        cloudflared_path = ROOT / "cloudflared.exe"
    else:
        cloudflared_path = ROOT / "cloudflared"

    if not cloudflared_path.exists():
        print(f"⚠️  {cloudflared_path.name} not found in root. UI Tunnel will not start.")
        print("   Run `python scripts/install.py` to download it.")
        tunnel_cmd = None
    else:
        # Tunnel ID/Name from your prompt
        tunnel_cmd = [str(cloudflared_path), "tunnel", "run", "tomcat-ui"]

    print(f"Starting TomCat ecosystem (cwd={ROOT})...")
    
    processes = []

    try:
        # Start API/Bot
        print(f"→ Bot: {' '.join(bot_cmd)}")
        bot_proc = subprocess.Popen(bot_cmd, cwd=ROOT)
        processes.append(bot_proc)

        # Start Tunnel (if available)
        if tunnel_cmd:
            print(f"→ Tunnel: {' '.join(tunnel_cmd)}")
            # Redirecting tunnel stderr to allow user to see connection logs if needed
            tunnel_proc = subprocess.Popen(tunnel_cmd, cwd=ROOT)
            processes.append(tunnel_proc)
        
        print("\n✅ TomCat is running. Press Ctrl+C to stop.\n")

        # Wait for bot to exit (or crash)
        exit_code = bot_proc.wait()
        
        if exit_code != 0:
            print(f"⚠️ Bot exited with code {exit_code}")

    except KeyboardInterrupt:
        print("\n🛑 Shutting down...")
    finally:
        # Kill everything on exit
        for p in processes:
            if p.poll() is None:
                # On Windows, terminate() is usually enough, but we want to be sure
                p.terminate()
                try:
                    p.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    p.kill()

if __name__ == "__main__":
    main()