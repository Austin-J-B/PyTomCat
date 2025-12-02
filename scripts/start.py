#!/usr/bin/env python3
"""Launch TomCat using the project virtual environment.

Starts:
  1. The Discord Bot (API Server) on port 8080
  2. The Cloudflare Tunnel pointing to localhost:8080
"""

from __future__ import annotations

import os
import subprocess
import sys
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
VENV_DIR = ROOT / ".venv"
CONFIG_PATH = ROOT / "config.yml"
ENV_PATH = ROOT / ".env"

def _venv_python() -> Path:
    if os.name == "nt":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"

def _get_env_var(key: str, default: str = "") -> str:
    """Simple parser to grab a value from .env without external deps."""
    if not ENV_PATH.exists():
        return default
    
    content = ENV_PATH.read_text(encoding="utf-8")
    # Grab the line, strip comments and quotes
    pattern = f"^{key}\\s*=\\s*(?:\"([^\"]*)\"|([^#\\n]*))"
    match = re.search(pattern, content, re.MULTILINE)
    if match:
        return (match.group(1) or match.group(2) or "").strip()
    return default

def _configure_tunnel() -> str | None:
    """
    Generates a config.yml that maps ALL public domains found in 
    UI_ALLOWED_ORIGINS to localhost:8080.
    """
    cf_dir = Path.home() / ".cloudflared"
    
    # 1. Find credentials
    creds_files = list(cf_dir.glob("*.json"))
    if not creds_files:
        print("\n[Tunnel] CRITICAL: No credentials found in ~/.cloudflared/")
        return None
    creds_path = creds_files[0]
    
    try:
        data = json.loads(creds_path.read_text(encoding="utf-8"))
        tunnel_id = data.get("TunnelID")
        
        # 2. Parse all domains from .env
        raw_origins_str = _get_env_var("UI_ALLOWED_ORIGINS", "ui.catsofuta.org")
        raw_origins = raw_origins_str.split(",")
        
        valid_hostnames = []
        for origin in raw_origins:
            # Clean up: remove http://, https://, trailing slashes
            clean = origin.strip().lower().replace("https://", "").replace("http://", "").strip("/")
            
            # Skip empty or local addresses
            if not clean or "localhost" in clean or "127.0.0.1" in clean:
                continue
            
            valid_hostnames.append(clean)
            
        if not valid_hostnames:
            print("[Tunnel] No public domains found. Defaulting to ui.catsofuta.org")
            valid_hostnames = ["ui.catsofuta.org"]
        
        # 3. Build Ingress Rules for EVERY domain
        # This covers both austin-j-b.github.io AND ui.catsofuta.org if listed.
        ingress_rules = ""
        for host in valid_hostnames:
            ingress_rules += f"  - hostname: {host}\n    service: http://localhost:8080\n"

        config_content = f"""
tunnel: {tunnel_id}
credentials-file: {str(creds_path.absolute()).replace(os.sep, '/')}

ingress:
{ingress_rules}
  - service: http_status:404
"""
        CONFIG_PATH.write_text(config_content, encoding="utf-8")
        print(f"[Tunnel] Generated config.yml for: {', '.join(valid_hostnames)}")
        return tunnel_id

    except Exception as e:
        print(f"[Tunnel] Config generation failed: {e}")
        return None

def main() -> None:
    yolo_dir = ROOT / ".config" / "ultralytics"
    yolo_dir.mkdir(parents=True, exist_ok=True)
    os.environ["YOLO_CONFIG_DIR"] = str(yolo_dir)
    
    python_path = _venv_python()
    if not python_path.exists():
        print("Virtual environment not found.")
        sys.exit(1)

    # --- 1. Configure Tunnel ---
    tunnel_uuid = _configure_tunnel()

    # --- 2. Commands ---
    bot_cmd = [str(python_path), "-m", "tomcat.main"]

    if os.name == "nt":
        cloudflared_path = ROOT / "cloudflared.exe"
    else:
        cloudflared_path = ROOT / "cloudflared"

    tunnel_cmd = None
    if cloudflared_path.exists() and tunnel_uuid:
        tunnel_cmd = [
            str(cloudflared_path), "tunnel", 
            "--config", str(CONFIG_PATH), 
            "run"
        ]

    # --- 3. Start ---
    print(f"Starting TomCat ecosystem...")
    processes = []
    try:
        print(f"→ Bot: {' '.join(bot_cmd)}")
        processes.append(subprocess.Popen(bot_cmd, cwd=ROOT))

        if tunnel_cmd:
            print(f"→ Tunnel: {' '.join(tunnel_cmd)}")
            processes.append(subprocess.Popen(tunnel_cmd, cwd=ROOT))
        
        processes[0].wait() # Wait for bot
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        for p in processes:
            p.terminate()

if __name__ == "__main__":
    main()