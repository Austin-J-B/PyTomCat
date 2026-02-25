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
import shutil
import time
import socket
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
    #Grab the line, strip comments and quotes
    pattern = f"^{key}\\s*=\\s*(?:\"([^\"]*)\"|([^#\\n]*))"
    match = re.search(pattern, content, re.MULTILINE)
    if match:
        return (match.group(1) or match.group(2) or "").strip()
    return default

def _read_config_tunnel_id() -> str | None:
    if not CONFIG_PATH.exists():
        return None
    for line in CONFIG_PATH.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("tunnel:"):
            return stripped.split(":", 1)[1].strip()
    return None


def _wait_for_local_port(host: str, port: int, timeout_sec: float = 25.0) -> bool:
    """Poll a local TCP endpoint until it accepts connections or timeout."""
    deadline = time.time() + max(0.5, float(timeout_sec))
    while time.time() < deadline:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.settimeout(0.5)
            if sock.connect_ex((host, int(port))) == 0:
                return True
        except Exception:
            pass
        finally:
            try:
                sock.close()
            except Exception:
                pass
        time.sleep(0.2)
    return False

def _resolve_credentials_path(raw_value: str) -> Path:
    candidate = Path(raw_value.strip())
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    return candidate.resolve()

def _maybe_create_tunnel(
    cloudflared_path: Path | None, cf_dir: Path, tunnel_name: str
) -> Path | None:
    if not cloudflared_path or not cloudflared_path.exists():
        return None
    existing = {p.name for p in cf_dir.glob("*.json")}
    try:
        subprocess.run(
            [str(cloudflared_path), "tunnel", "create", tunnel_name], check=True
        )
    except subprocess.CalledProcessError:
        return None
    created = [p for p in cf_dir.glob("*.json") if p.name not in existing]
    return created[0] if created else None

def _configure_tunnel(cloudflared_path: Path | None) -> str | None:
    """
    Generates a config.yml that maps ALL public domains found in 
    UI_ALLOWED_ORIGINS to localhost:8080.
    """
    cf_dir = Path.home() / ".cloudflared"
    
    #1. Find credentials
    creds_path: Path | None = None
    env_creds_path = _get_env_var("CLOUDFLARE_TUNNEL_CREDENTIALS")
    if env_creds_path:
        candidate = _resolve_credentials_path(env_creds_path)
        if candidate.exists():
            creds_path = candidate
        else:
            print(f"[Tunnel] Credentials file not found: {candidate}")

    env_tunnel_id = _get_env_var("CLOUDFLARE_TUNNEL_ID")
    if not creds_path and env_tunnel_id:
        candidate = cf_dir / f"{env_tunnel_id}.json"
        if candidate.exists():
            creds_path = candidate
        else:
            print(f"[Tunnel] Credentials not found for tunnel ID: {env_tunnel_id}")

    expected_id = _read_config_tunnel_id()
    if not creds_path and expected_id:
        candidate = cf_dir / f"{expected_id}.json"
        if candidate.exists():
            creds_path = candidate

    if not creds_path:
        creds_files = list(cf_dir.glob("*.json"))
        if creds_files:
            creds_path = creds_files[0]
            if len(creds_files) > 1:
                names = ", ".join(p.name for p in creds_files)
                print(f"[Tunnel] Multiple credentials found; using {creds_path.name} ({names})")
        else:
            tunnel_name = _get_env_var("CLOUDFLARE_TUNNEL_NAME")
            if tunnel_name:
                created = _maybe_create_tunnel(cloudflared_path, cf_dir, tunnel_name)
                if created:
                    creds_path = created

    if not creds_path:
        print("\n[Tunnel] CRITICAL: No credentials found in ~/.cloudflared/")
        if expected_id:
            print(f"[Tunnel] Expected tunnel ID from config.yml: {expected_id}")
        print("Copy <TunnelID>.json from the original machine's ~/.cloudflared into this machine.")
        print("Or set CLOUDFLARE_TUNNEL_CREDENTIALS in .env to point at the JSON file.")
        print("If you want new credentials, run `cloudflared tunnel create <name>` after login.")
        return None
    
    try:
        data = json.loads(creds_path.read_text(encoding="utf-8"))
        tunnel_id = data.get("TunnelID")
        
        #2. Parse all domains from .env
        raw_origins_str = _get_env_var("UI_ALLOWED_ORIGINS", "ui.catsofuta.org")
        raw_origins = raw_origins_str.split(",")
        
        valid_hostnames = []
        for origin in raw_origins:
            #Clean up: remove http://, https://, trailing slashes
            clean = origin.strip().lower().replace("https://", "").replace("http://", "").strip("/")
            
            #Skip empty or local addresses
            if not clean or "localhost" in clean or "127.0.0.1" in clean:
                continue
            
            valid_hostnames.append(clean)
            
        if not valid_hostnames:
            print("[Tunnel] No public domains found. Defaulting to ui.catsofuta.org")
            valid_hostnames = ["ui.catsofuta.org"]
        
        #3. Build Ingress Rules for EVERY domain
        #This covers both austin-j-b.github.io AND ui.catsofuta.org if listed.
        ingress_rules = ""
        for host in valid_hostnames:
            ingress_rules += f"  - hostname: {host}\n    service: http://127.0.0.1:8080\n"

        config_content = f"""
tunnel: {tunnel_id}
credentials-file: {str(creds_path.resolve()).replace(os.sep, '/')}

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
    yolo_dir = ROOT / ".ultra"
    legacy_dir = ROOT.with_suffix(".ultra")
    if legacy_dir.exists() and not yolo_dir.exists():
        try:
            shutil.copytree(legacy_dir, yolo_dir, dirs_exist_ok=True)
            print(f"[YOLO] Migrated Ultralytics cache to {yolo_dir}")
        except Exception as exc:
            print(f"[YOLO] Failed to migrate legacy cache: {exc}")
    yolo_dir.mkdir(parents=True, exist_ok=True)
    os.environ["YOLO_CONFIG_DIR"] = str(yolo_dir)
    
    python_path = _venv_python()
    if not python_path.exists():
        print("Virtual environment not found.")
        sys.exit(1)

    if os.name == "nt":
        cloudflared_path = ROOT / "cloudflared.exe"
    else:
        cloudflared_path = ROOT / "cloudflared"

    #--- 1. Configure Tunnel ---
    tunnel_uuid = _configure_tunnel(cloudflared_path if cloudflared_path.exists() else None)

    #--- 2. Commands ---
    bot_cmd = [str(python_path), "-m", "tomcat.main"]

    tunnel_cmd = None
    if cloudflared_path.exists() and tunnel_uuid:
        #Auto-update cloudflared before starting
        try:
            subprocess.run([str(cloudflared_path), "update"], check=False)
        except Exception as exc:
            print(f"[Tunnel] Update check failed: {exc}")
        tunnel_cmd = [
            str(cloudflared_path), "tunnel", 
            "--config", str(CONFIG_PATH), 
            "run"
        ]

    #--- 3. Start ---
    print(f"Starting TomCat ecosystem...")
    processes = []
    try:
        print(f"-> Bot: {' '.join(bot_cmd)}")
        processes.append(subprocess.Popen(bot_cmd, cwd=ROOT))

        if tunnel_cmd:
            if _wait_for_local_port("127.0.0.1", 8080, timeout_sec=30.0):
                print(f"-> Tunnel: {' '.join(tunnel_cmd)}")
                # Keep cloudflared connection churn out of interactive terminal.
                processes.append(
                    subprocess.Popen(
                        tunnel_cmd,
                        cwd=ROOT,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                )
            else:
                print("[Tunnel] Skipped start because 127.0.0.1:8080 is not ready.")
        
        processes[0].wait() #Wait for bot
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        for p in processes:
            try:
                if p.poll() is None:
                    p.terminate()
            except Exception:
                pass

        deadline = time.time() + 8.0
        for p in processes:
            if p.poll() is not None:
                continue
            timeout_left = max(0.2, deadline - time.time())
            try:
                p.wait(timeout=timeout_left)
            except subprocess.TimeoutExpired:
                try:
                    p.kill()
                except Exception:
                    pass

        for p in processes:
            if p.poll() is None:
                try:
                    p.wait(timeout=1.0)
                except Exception:
                    pass

if __name__ == "__main__":
    main()
