#!/usr/bin/env python3
"""One-stop installer for TomCat.

This script provisions the local environment so the bot can run on a fresh
machine. It includes protections against corrupted caches, incompatible
Python versions, and handles Cloudflare authentication automatically.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Iterable, List

#======================
#Installation configuration
#======================
ROOT = Path(__file__).resolve().parent.parent
VENV_DIR = ROOT / ".venv"
WEIGHTS_DIR = ROOT / "weights"
ONNX_PATH = WEIGHTS_DIR / "deberta-v3-small-mnli.onnx"
TOKENIZER_PATH = WEIGHTS_DIR / "deberta-v3-small-mnli.tokenizer.json"
CONFIG_PATH = ROOT / "config.yml"

REQUIRED_WEIGHTS = ["NanoModel.pt", "NanoClassifier.pt"]

#PyTorch specifications for CPU and GPU (CUDA 12.1)
TORCH_CPU_SPEC = ["torch==2.4.1", "torchvision==0.19.1"]
TORCH_GPU_SPEC = [
    "--extra-index-url",
    "https://download.pytorch.org/whl/cu121",
    "torch==2.4.1+cu121",
    "torchvision==0.19.1+cu121",
]

#Environment template used to scaffold a fresh .env file
ENV_TEMPLATE_CONTENT = """# ===== Discord =====
DISCORD_TOKEN=
DISCORD_CLIENT_SECRET=
UI_SESSION_SECRET=                    # Required: secret for signing UI session cookies
UI_ALLOWED_ORIGINS=                   # Should match the Discord OAuth2 dev portal
UI_AUTH_DEBUG=false                   # Enable verbose auth/CORS logging when true
UI_GUILD_ID=                          # Optional: override guild ID to validate membership for UI auth
UI_COOKIE_SECURE=true                 # Use secure cookies (SameSite=None) so cross-site pages send session
COMMAND_PREFIX=!                      # Command prefix used by commands.Bot
TOMCAT_WAKE=TomCat
TIMEZONE=America/Chicago

# Comma-separated admin user IDs who can run privileged commands
ADMIN_IDS=
# Comma-separated role IDs that can ban spammers via reaction
SPAM_BAN_ROLE_IDS=

# ===== Channels (Discord snowflakes) =====
CH_FEEDING_TEAM=
CH_TOMCAT_SANDBOX=
CH_PICTURES_OF_CATS=
CH_REPORT_NEW_CATS=
CH_DUE_PORTAL=
CH_LOGGING=
CH_CATDOCDUMP=
CH_MEMBER_NAMES=
TARGET_GUILD_ID=
CHANNEL_SHEET_MAP=CH_PICTURES_OF_CATS:TCBPicsInput, CH_REPORT_NEW_CATS:TCBPicsInput, CH_TOMCAT_SANDBOX:TCBPicsInput, CH_CATDOCDUMP:TCBVetBillInput
allowed_feeding_channel_ids=[CH_FEEDING_TEAM, CH_TOMCAT_SANDBOX]

# Ping target when a spam message is detected
SPAM_ALERT_USER_ID=

# ===== Google Service Account =====
GOOGLE_SERVICE_ACCOUNT_JSON=./credentials/service_account.json

# ===== Sheets =====
SHEET_CATABASE_ID=                    # Catabase (cat bios + latest image URL)
SHEET_VISION_ID=                      # Vision/aux (RecentPics, FeedingStationChecklist, etc.)
SHEET_MEGASHEET_ID=                   # Members/finance megasheet (used for dues/membership)
MEMBERSHIP_WS_TITLE="Membership Application List"

# ===== ML Model Paths =====
NLP_MODEL_PATH=weights/deberta-v3-small-mnli.onnx
NLP_TOKENIZER_PATH=weights/deberta-v3-small-mnli.tokenizer.json
CV_DETECT_WEIGHTS=weights/NanoModel.pt
CV_CLASSIFY_WEIGHTS=weights/NanoClassifier.pt
CV_MAX_DOWNLOAD_MB=0                  # Set to 0 to lift the max file size limit

# ===== Gmail Ingestion =====
GMAIL_ENABLED=true                    # Toggle Gmail integration; disable to skip email polling entirely
GMAIL_CREDENTIALS_PATH=credentials/gmail_oauth_client.json  # OAuth client secrets downloaded from Google Cloud
GMAIL_TOKEN_PATH=credentials/gmail_token.json              # Stored refresh/access token generated after auth
GMAIL_LOCAL_PORT=8765                 # Local redirect port the OAuth helper spins up

# ===== Dues Processing =====
DUES_ENABLED=true                     # Master switch for dues automation
DUES_ALLOWED_AMOUNTS=15               # Base dues amount (comma-list if multiples allowed)
DUES_EMAIL_WINDOW_DAYS=5              # Only consider payment emails this many days back
DUES_SCAN_SKIP_OLDEST=3               # Skip this many oldest sheet rows when scanning in batches
DUES_SCAN_LIMIT=500                   # Cap rows reviewed per pass to limit Sheets API usage
DUES_NLP_ENABLED=true                 # Enable NLP heuristics for intent/donation parsing
DUES_MEMBERSHIP_TTL_SEC=300           # Cache TTL for dues membership lookups (seconds)
DUES_FAST_MAP=1                       # Use fast user map resolver (set 0 to force slower fallback)
DUES_AUTO_VERIFY_THRESHOLD=0.90       # Auto-verify members with score >= this threshold

# ===== Bot Identity =====
BOT_USER_ID=                          # Bot user ID for mention detection + wake word shortcuts

# ===== Cache Expiry =====
CAT_PROFILE_TTL_SEC=3600              # Seconds before cached cat profiles auto-refresh
CAT_ALIASES_TTL_SEC=7200              # Seconds before alias table refreshes from Sheets
FINANCE_SHEET_THROTTLE_SEC=0.5        # When finances are requested, it waits this long between queries

# ===== UI / Role IDs =====
UITEST_ACTIVITY_APP_ID=               # For the user interface to work
OFFICER_ROLE_ID=                      # Required: officer role for UI/admin actions
ROLE_FEEDING_MANAGER=                 # Feeding manager role ID
ROLE_PHOTO_LABELER=                   # Photo labeler role ID
ROLE_VIEWER=                          # Basic viewer role ID
ROLE_DUE_PAYING=                      # Due-paying member role ID
ROLE_HOLIDAY_FEEDER=                  # Holiday feeder role ID
ROLE_DUES_PERKS=                      # Role granted by dues perks workflow

# ===== Cloudflare Tunnel =====
CLOUDFLARE_TUNNEL_ID=                 # Tunnel ID used to locate ~/.cloudflared/<id>.json
CLOUDFLARE_TUNNEL_CREDENTIALS=        # Optional path to tunnel credentials JSON
CLOUDFLARE_TUNNEL_NAME=               # Optional: create tunnel if credentials are missing
"""
ENV_TEMPLATE_PATH = ROOT / ".env TEMPLATE"

class InstallError(RuntimeError):
    """Raised when a provisioning step cannot be recovered automatically."""

def _print_header(title: str) -> None:
    bar = "=" * len(title)
    print(f"\n{bar}\n{title}\n{bar}\n")

def _run(cmd: List[str], *, cwd: Path | None = None, env: dict | None = None) -> None:
    pretty = " ".join(cmd)
    print(f"→ {pretty}")
    subprocess.run(cmd, cwd=cwd, env=env, check=True)

def _venv_python() -> Path:
    if os.name == "nt":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"

def _bootstrap_python_windows() -> None:
    """Downloads and installs Python 3.12 if the current version is incompatible."""
    _print_header("Python Bootstrapper")
    url = "https://www.python.org/ftp/python/3.12.7/python-3.12.7-amd64.exe"
    installer_path = ROOT / "python_installer.exe"
    
    print(f"Current Python {sys.version_info.major}.{sys.version_info.minor} is unsupported.")
    print("Downloading Python 3.12.7 (64-bit)...")
    
    try:
        urllib.request.urlretrieve(url, installer_path)
        print("Starting installer. Ensure 'Add Python to PATH' is checked in the window.")
        #Trigger installer with PrependPath for easier command line use
        subprocess.run([str(installer_path), "/passive", "PrependPath=1"], check=True)
        print("Installation finished. Restart your terminal and run this script again.")
        installer_path.unlink(missing_ok=True)
        sys.exit(0)
    except Exception as e:
        raise InstallError(f"Bootstrapper failed: {e}")

def _clean_hf_cache() -> None:
    targets = [
        Path(os.path.expanduser("~")) / ".cache" / "huggingface",
        Path(os.path.expanduser("~")) / ".huggingface",
    ]
    for path in targets:
        try:
            if path.exists():
                shutil.rmtree(path, ignore_errors=True)
                print(f"Cleared {path}")
        except Exception as exc:
            print(f"Warning: failed to remove {path}: {exc}")

def _ensure_repo() -> None:
    if not (ROOT / "requirements.txt").exists():
        raise InstallError(
            "requirements.txt not found. Run this script from the repository root."
        )

def _ensure_cloudflared() -> Path:
    _print_header("Checking Cloudflare Tunnel Binary")
    if os.name == "nt":
        filename = "cloudflared.exe"
        url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
    else:
        filename = "cloudflared"
        url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"

    target_path = ROOT / filename
    if target_path.exists():
        print(f"{filename} is already present.")
        return target_path

    print(f"Downloading {filename} from {url}...")
    try:
        urllib.request.urlretrieve(url, target_path)
        if os.name != "nt":
            target_path.chmod(0o755)
        print(f"Downloaded {filename} to {target_path}")
        return target_path
    except Exception as exc:
        print(f"Failed to download cloudflared: {exc}")
        print("Manual download may be required for UI tunnel.")
        return target_path

def _ensure_cloudflared_auth(bin_path: Path) -> None:
    if not bin_path.exists():
        return

    cert_path = Path.home() / ".cloudflared" / "cert.pem"
    if cert_path.exists():
        print(f"✅ Cloudflare authentication found at: {cert_path}")
        return

    _print_header("Authenticating Cloudflare Tunnel")
    print("No existing Cloudflare login found.")
    print("Follow the instructions in the browser window to log in.")

    try:
        subprocess.run([str(bin_path), "tunnel", "login"], check=True)
        print("\nCloudflare login successful!")
    except subprocess.CalledProcessError:
        print("\nCloudflare login failed or was cancelled.")

def _read_expected_tunnel_id() -> str | None:
    if not CONFIG_PATH.exists():
        return None
    try:
        for line in CONFIG_PATH.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("tunnel:"):
                return stripped.split(":", 1)[1].strip()
    except Exception:
        return None
    return None

def _ensure_cloudflared_credentials(
    bin_path: Path,
    creds_source: Path | None,
    tunnel_name: str | None,
) -> None:
    if not bin_path.exists():
        return

    cf_dir = Path.home() / ".cloudflared"
    cf_dir.mkdir(exist_ok=True)

    creds_files = list(cf_dir.glob("*.json"))
    if creds_files:
        names = ", ".join(p.name for p in creds_files)
        print(f"Cloudflare tunnel credentials found: {names}")
        return

    _print_header("Cloudflare Tunnel Credentials")
    expected_id = _read_expected_tunnel_id()

    if creds_source:
        if not creds_source.exists():
            print(f"Credentials file not found: {creds_source}")
        else:
            target = cf_dir / creds_source.name
            shutil.copy2(creds_source, target)
            print(f"Copied tunnel credentials to: {target}")
            return

    if tunnel_name:
        try:
            subprocess.run([str(bin_path), "tunnel", "create", tunnel_name], check=True)
        except subprocess.CalledProcessError:
            print("Cloudflare tunnel creation failed.")
        else:
            creds_files = list(cf_dir.glob("*.json"))
            if creds_files:
                names = ", ".join(p.name for p in creds_files)
                print(f"Created tunnel credentials: {names}")
                return

    print("No Cloudflare tunnel credentials found.")
    if expected_id:
        print(f"Expected tunnel ID from config.yml: {expected_id}")
        print(f"Copy {expected_id}.json into {cf_dir}")

def _create_or_reuse_venv(python_exe: Path) -> None:
    #Nuke existing .venv if it is broken or points to a missing interpreter
    if VENV_DIR.exists():
        vpython = _venv_python()
        if not vpython.exists():
            print("Virtual environment is broken. Recreating...")
            shutil.rmtree(VENV_DIR)

    if _venv_python().exists():
        print("Virtual environment already present – reusing .venv")
        return

    _print_header("Creating virtual environment")
    _run([str(python_exe), "-m", "venv", str(VENV_DIR)])

def _pip(args: Iterable[str]) -> None:
    #Disabled cache to prevent permission issues on corrupted system files
    base_cmd = [str(_venv_python()), "-m", "pip", "install", "--no-cache-dir"]
    clean_args = [a for a in args if a != "install"]
    _run(base_cmd + clean_args)

def _install_base_dependencies(force_reinstall: bool = False) -> None:
    _print_header("Installing Python dependencies")
    _pip(["--upgrade", "pip", "setuptools", "wheel"])

    req_path = ROOT / "requirements.txt"
    base_packages: List[str] = []
    with req_path.open("r", encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            lower = line.lower()
            #Torch handled in a separate dedicated step
            if "torch" in lower or lower.startswith("--extra-index-url"):
                continue
            base_packages.append(line)

    if base_packages:
        args = []
        if force_reinstall:
            args.extend(["--upgrade", "--force-reinstall"])
        args.extend(base_packages)
        _pip(args)

def _detect_cuda() -> bool:
    _print_header("Hardware Detection")
    if shutil.which("nvidia-smi"):
        print("NVIDIA GPU detected.")
        return True
    print("ℹNo NVIDIA GPU found; using CPU wheels.")
    return False

def _install_torch(force: str | None = None) -> None:
    wants_gpu: bool
    if force == "gpu":
        wants_gpu = True
    elif force == "cpu":
        wants_gpu = False
    else:
        wants_gpu = _detect_cuda()

    spec = TORCH_GPU_SPEC if wants_gpu else TORCH_CPU_SPEC
    
    _print_header(f"Installing PyTorch ({'GPU/CUDA 12.1' if wants_gpu else 'CPU'})")
    try:
        _pip(spec)
    except subprocess.CalledProcessError:
        if wants_gpu:
            print("⚠️ GPU install failed; falling back to CPU...")
            _pip(TORCH_CPU_SPEC)
            return
        raise

def _ensure_extra_models() -> None:
    _pip([
        "huggingface_hub>=0.35.1,<0.36",
        "transformers==4.43.3",
        "safetensors>=0.4.4",
        "sentencepiece>=0.1.99",
        "onnx>=1.16.2,<1.17",
        "onnxscript" 
    ])

def _cleanup_tokenizer_artifacts() -> None:
    leftovers = [
        WEIGHTS_DIR / "added_tokens.json",
        WEIGHTS_DIR / "special_tokens_map.json",
        WEIGHTS_DIR / "spm.model",
        WEIGHTS_DIR / "tokenizer_config.json",
    ]
    for path in leftovers:
        try:
            if path.exists():
                path.unlink()
        except Exception:
            pass

def _ensure_deberta_model() -> None:
    WEIGHTS_DIR.mkdir(exist_ok=True)
    if ONNX_PATH.exists() and TOKENIZER_PATH.exists():
        print("DeBERTa model files are present.")
        return
    _print_header("Downloading & converting DeBERTa (MNLI) model")
    _ensure_extra_models()
    _run([str(_venv_python()), "scripts/convert_model.py"], cwd=ROOT)
    _cleanup_tokenizer_artifacts()

def _check_yolo_weights() -> None:
    missing = []
    for w in REQUIRED_WEIGHTS:
        if not (WEIGHTS_DIR / w).exists():
            missing.append(w)
    
    if missing:
        _print_header("MISSING WEIGHTS")
        print(f"The following files are missing in {WEIGHTS_DIR}:")
        for m in missing:
            print(f"  - {m}")
        print("\nManually copy these from backup or Google Drive.")

def _test_model() -> None:
    _print_header("Validating model")
    _run([str(_venv_python()), "scripts/test_model.py"], cwd=ROOT)

def _maybe_create_env_template() -> None:
    env_path = ROOT / ".env"
    if env_path.exists():
        print(f"[info] {env_path.name} already exists.")
        return

    content = ENV_TEMPLATE_PATH.read_text(encoding="utf-8") if ENV_TEMPLATE_PATH.exists() else ENV_TEMPLATE_CONTENT
    env_path.write_text(content, encoding="utf-8")
    print(f"\n[ok] Created {env_path.name}.")
    print("ACTION REQUIRED: Fill in DISCORD_TOKEN and DISCORD_CLIENT_SECRET in .env.")

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Provision TomCat locally")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--cpu", action="store_true", help="force CPU wheels")
    group.add_argument("--gpu", action="store_true", help="force GPU wheels")
    parser.add_argument("--python", type=Path, help="specific Python path")
    parser.add_argument("--reinstall", action="store_true", help="force full reinstall")
    parser.add_argument("--resume-model", action="store_true", help="resume model steps")
    parser.add_argument("--skip-model", action="store_true", help="skip model steps")
    parser.add_argument("--clean-hf-cache", action="store_true", help="clear HF caches")
    parser.add_argument("--tunnel-credentials", type=Path, help="path to CF credentials")
    parser.add_argument("--tunnel-name", help="name for new CF tunnel")
    return parser.parse_args()

def main() -> None:
    args = _parse_args()
    python_exe = args.python.resolve() if args.python else Path(sys.executable)
    
    _print_header("TomCat Installer")
    print(f"Root: {ROOT}")
    print(f"Python: {python_exe}")

    #Check for compatibility; bootstrap if on Windows and outside support range
    if sys.version_info >= (3, 13) or sys.version_info < (3, 11):
        if os.name == "nt":
            _bootstrap_python_windows()
        else:
            raise InstallError(f"Python {sys.version_info.major}.{sys.version_info.minor} is unsupported. Install 3.12.")

    _ensure_repo()
    cf_path = _ensure_cloudflared()
    _ensure_cloudflared_auth(cf_path)
    _ensure_cloudflared_credentials(cf_path, args.tunnel_credentials.resolve() if args.tunnel_credentials else None, args.tunnel_name)

    if args.resume_model:
        if not _venv_python().exists():
            raise InstallError(".venv not found.")
        if args.clean_hf_cache:
            _clean_hf_cache()
        _ensure_deberta_model()
        _test_model()
        _check_yolo_weights()
        _maybe_create_env_template()
    else:
        _create_or_reuse_venv(python_exe)
        torch_force = "gpu" if args.gpu else "cpu" if args.cpu else None
        _install_torch(torch_force)

        try:
            _install_base_dependencies(force_reinstall=args.reinstall)
        except subprocess.CalledProcessError:
            print("\nDependency install failed. Recreating .venv...")
            if VENV_DIR.exists():
                shutil.rmtree(VENV_DIR)
            _create_or_reuse_venv(python_exe)
            _install_torch(torch_force)
            _install_base_dependencies(force_reinstall=True)

        if not args.skip_model:
            if args.clean_hf_cache:
                _clean_hf_cache()
            _ensure_deberta_model()
            _test_model()

        _check_yolo_weights()
        _maybe_create_env_template()

    print("\nInstallation Complete!")
    print("1. Fill in DISCORD_TOKEN and DISCORD_CLIENT_SECRET in '.env'.")
    print("2. Run the bot: python scripts/start.py\n")

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Error: {exc}")
        sys.exit(1)