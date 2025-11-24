#!/usr/bin/env python3
"""Launch TomCat using the project virtual environment.

Starts:
  1. The Discord Bot (API Server)
  2. The Cloudflare Tunnel (tomcat-ui) with explicit config
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
VENV_DIR = ROOT / ".venv"
CONFIG_PATH = ROOT / "config.yml"


def _venv_python() -> Path:
    if os.name == "nt":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def _check_tunnel_creds() -> None:
    """Verify that Cloudflare tunnel credentials exist in the user's home directory."""
    # Cloudflare stores tunnel credentials in ~/.cloudflared/
    cf_dir = Path.home() / ".cloudflared"
    
    # We look for any .json file (which are the tunnel credentials).
    # If the folder doesn't exist or is empty of JSONs, the tunnel will fail.
    has_creds = False
    if cf_dir.exists():
        if any(cf_dir.glob("*.json")):
            has_creds = True
            
    if not has_creds:
        print("\n❌ CRITICAL ERROR: Cloudflare Tunnel Credentials Not Found!")
        print(f"   Checked in: {cf_dir}")
        print("   The bot cannot launch the website without the tunnel key.")
        print("\n👉 TROUBLESHOOTING: Have you copied the .json tunnel secret to this machine when first installed?")
        print("   (Copy the *.json file from your old computer's .cloudflared folder to this one)\n")
        sys.exit(1)


def main() -> None:
    python_path = _venv_python()
    if not python_path.exists():
        print("❌ Virtual environment not found. Run `python scripts/install.py` first.")
        sys.exit(1)

    # --- PRE-FLIGHT CHECKS ---
    # Check for tunnel credentials before trying to start
    _check_tunnel_creds()

    # 1. Prepare Bot Command
    bot_cmd = [str(python_path), "-m", "tomcat.main"]

    # 2. Prepare Cloudflare Tunnel Command
    if os.name == "nt":
        cloudflared_path = ROOT / "cloudflared.exe"
    else:
        cloudflared_path = ROOT / "cloudflared"

    if not cloudflared_path.exists():
        print(f"⚠️  {cloudflared_path.name} not found in root. UI Tunnel will not start.")
        tunnel_cmd = None
    else:
        # Check for config.yml
        if not CONFIG_PATH.exists():
            print("⚠️  config.yml not found in root. Tunnel will likely fail to route traffic.")
            # We try to run anyway, but warn the user
            tunnel_cmd = [str(cloudflared_path), "tunnel", "run", "tomcat-ui"]
        else:
            # FORCE cloudflared to use our local config.yml
            tunnel_cmd = [
                str(cloudflared_path), 
                "tunnel", 
                "--config", str(CONFIG_PATH), 
                "run", "tomcat-ui"
            ]

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
                p.terminate()
                try:
                    p.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    p.kill()

if __name__ == "__main__":
    main()