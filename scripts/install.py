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

# --- CONFIGURATION ---
ROOT = Path(__file__).resolve().parent.parent
VENV_DIR = ROOT / ".venv"
WEIGHTS_DIR = ROOT / "weights"
ONNX_PATH = WEIGHTS_DIR / "deberta-v3-small-mnli.onnx"
TOKENIZER_PATH = WEIGHTS_DIR / "deberta-v3-small-mnli.tokenizer.json"

REQUIRED_WEIGHTS = ["NanoModel.pt", "NanoClassifier.pt"]

# PyTorch configuration
TORCH_CPU_SPEC = ["torch==2.3.1", "torchvision==0.18.1"]
TORCH_GPU_SPEC = [
    "--extra-index-url",
    "https://download.pytorch.org/whl/cu121",
    "torch==2.3.1+cu121",
    "torchvision==0.18.1+cu121",
]

# The template provided by the user
ENV_TEMPLATE_CONTENT = """# ===== Discord =====
DISCORD_TOKEN=#
DISCORD_CLIENT_SECRET=#
COMMAND_PREFIX=! # Command prefix used by commands.Bot
TOMCAT_WAKE=TomCat
TIMEZONE=America/Chicago
#Austin, Megan, Derek, Cel, Kaz, Jesse, McKayla, Bel, and Jacob
ADMIN_IDS=624440365595754496,499034835562790912,217081575873970178,429741733706989568,1361167705897435229,244678835889635328,607002156247154688,837445695480135691,503377157830082560

# ===== Channels (Discord snowflakes) =====
CH_FEEDING_TEAM=643586809166561310
CH_TOMCAT_SANDBOX=1341696618688286720
CH_PICTURES_OF_CATS=551084964318543904
CH_REPORT_NEW_CATS=639882573199441930
CH_DUE_PORTAL=928060549089087538
CH_LOGGING=842975801934217276
CH_CATDOCDUMP=1344745306620694558
CHANNEL_SHEET_MAP=CH_PICTURES_OF_CATS:TCBPicsInput, CH_REPORT_NEW_CATS:TCBPicsInput, CH_TOMCAT_SANDBOX:TCBPicsInput, CH_CATDOCDUMP:TCBVetBillInput
allowed_feeding_channel_ids=[CH_FEEDING_TEAM, CH_TOMCAT_SANDBOX]
CH_MEMBER_NAMES=933458020892033084

# Ping target when a spam message is detected
SPAM_ALERT_USER_ID=624440365595754496

# ===== Google Service Account =====
GOOGLE_SERVICE_ACCOUNT_JSON=./credentials/service_account.json

# ===== Sheets =====
# Catabase (cat bios + latest image URL)
SHEET_CATABASE_ID=15HtHVB4HfOr9e85EgbOGbz9CBwWnXg3pbCytGTA64P4 #Catabase
# Vision/aux (RecentPics, FeedingStationChecklist, etc.)
SHEET_VISION_ID=1ypMoqpB0XbiVVhJ1GP_6gVcqMmt-FXRrkoO2D2fC8WE  #TomCatVision
# Members/finance megasheet (used later for dues/membership)
SHEET_MEGASHEET_ID=1PBvCd6gTwc1_aqlCtn93xPW7XJOHclseY2tlxDk9whA # CCC megasheet
MEMBERSHIP_WS_TITLE="Membership Application List"

NLP_MODEL_PATH=weights/deberta-v3-small-mnli.onnx
NLP_TOKENIZER_PATH=weights/deberta-v3-small-mnli.tokenizer.json
CV_DETECT_WEIGHTS=weights/NanoModel.pt
CV_CLASSIFY_WEIGHTS=weights/NanoClassifier.pt

CV_MAX_DOWNLOAD_MB=0 # Set to 0 to lift the max file size limit

# Gmail ingestion
GMAIL_ENABLED=true                    # Toggle Gmail integration; disable to skip email polling entirely
GMAIL_CREDENTIALS_PATH=credentials/gmail_oauth_client.json  # OAuth client secrets downloaded from Google Cloud
GMAIL_TOKEN_PATH=credentials/gmail_token.json              # Stored refresh/access token generated after auth
GMAIL_LOCAL_PORT=8765                                      # Local redirect port the OAuth helper spins up

# Dues processing knobs
DUES_ENABLED=true                     # Master switch for dues automation
DUES_ALLOWED_AMOUNTS=15               # Base dues amount (comma-list if multiples allowed)
DUES_EMAIL_WINDOW_DAYS=5              # Only consider payment emails this many days back
DUES_SCAN_SKIP_OLDEST=3               # Skip this many oldest sheet rows when scanning in batches
DUES_SCAN_LIMIT=500                   # Cap rows reviewed per pass to limit Sheets API usage
DUES_NLP_ENABLED=true                 # Enable NLP heuristics for intent/donation parsing
DUES_MEMBERSHIP_TTL_SEC=300           # Cache TTL for dues membership lookups (seconds)
DUES_FAST_MAP=1                       # Use fast user map resolver (set 0 to force slower fallback)

# Discord bot identity
BOT_USER_ID=1341667150066225192       # Bot user ID for mention detection + wake word shortcuts

# Cache expiry settings
CAT_PROFILE_TTL_SEC=3600              # Seconds before cached cat profiles auto-refresh
CAT_ALIASES_TTL_SEC=7200              # Seconds before alias table refreshes from Sheets

FINANCE_SHEET_THROTTLE_SEC=0.5        # When finances are requested, it waits this long between queries.
UITEST_ACTIVITY_APP_ID=1341667150066225192 #For the user interface to work.
"""

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
        print(f"✅ {filename} is already present.")
        return target_path

    print(f"Downloading {filename} from {url}...")
    try:
        urllib.request.urlretrieve(url, target_path)
        if os.name != "nt":
            target_path.chmod(0o755)
        print(f"✅ Downloaded {filename} to {target_path}")
        return target_path
    except Exception as exc:
        print(f"⚠️ Failed to download cloudflared: {exc}")
        print("You may need to download it manually to run the UI tunnel.")
        return target_path

def _ensure_cloudflared_auth(bin_path: Path) -> None:
    if not bin_path.exists():
        return

    # Typical location for Cloudflare cert on Windows/Linux
    user_home = Path.home()
    cert_path = user_home / ".cloudflared" / "cert.pem"
    
    if cert_path.exists():
        print(f"✅ Cloudflare authentication found at: {cert_path}")
        return

    _print_header("Authenticating Cloudflare Tunnel")
    print("⚠️  No existing Cloudflare login found.")
    print("👉 A browser window will open shortly. Please log in to Cloudflare and select your domain.")
    print("   (Wait for the process to complete in the browser...)")

    try:
        subprocess.run([str(bin_path), "tunnel", "login"], check=True)
        print("\n✅ Cloudflare login successful!")
    except subprocess.CalledProcessError:
        print("\n❌ Cloudflare login failed or was cancelled.")
        print("   You may need to run `cloudflared.exe tunnel login` manually.")

def _create_or_reuse_venv(python_exe: Path) -> None:
    if _venv_python().exists():
        print("Virtual environment already present – reusing .venv")
        return
    _print_header("Creating virtual environment")
    _run([str(python_exe), "-m", "venv", str(VENV_DIR)])

def _pip(args: Iterable[str]) -> None:
    # We add --no-cache-dir to prevent "Permission denied" errors on corrupted cache files
    base_cmd = [str(_venv_python()), "-m", "pip", "install", "--no-cache-dir"]
    clean_args = [a for a in args if a != "install"]
    _run(base_cmd + clean_args)

def _install_base_dependencies(force_reinstall: bool = False) -> None:
    _print_header("Installing Python dependencies")
    # Upgrade pip first
    _pip(["--upgrade", "pip", "setuptools", "wheel"])

    req_path = ROOT / "requirements.txt"
    base_packages: List[str] = []
    with req_path.open("r", encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            lower = line.lower()
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
        print("✅ NVIDIA GPU detected via nvidia-smi.")
        return True
    print("ℹ️ No NVIDIA GPU detected – defaulting to CPU wheels.")
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
        return
    except subprocess.CalledProcessError:
        if wants_gpu:
            print("⚠️ GPU wheel install failed – retrying with CPU wheels as fallback...")
            _pip(TORCH_CPU_SPEC)
            return
        raise

def _ensure_extra_models() -> None:
    # Added 'onnxscript' here to fix the ModuleNotFoundError
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
        print("✅ DeBERTa ONNX model already present.")
        return
    _print_header("Downloading & converting DeBERTa (MNLI) ONNX model")
    _ensure_extra_models()
    _run([str(_venv_python()), "scripts/convert_model.py"], cwd=ROOT)
    _cleanup_tokenizer_artifacts()

def _check_yolo_weights() -> None:
    missing = []
    for w in REQUIRED_WEIGHTS:
        if not (WEIGHTS_DIR / w).exists():
            missing.append(w)
    
    if missing:
        _print_header("⚠️  MISSING WEIGHTS  ⚠️")
        print(f"The following model files were not found in {WEIGHTS_DIR}:")
        for m in missing:
            print(f"  - {m}")
        print("\nPlease manually copy these files from your backup or Google Drive.")

def _test_model() -> None:
    _print_header("Validating ONNX model")
    _run([str(_venv_python()), "scripts/test_model.py"], cwd=ROOT)

def _maybe_create_env_template() -> None:
    env_path = ROOT / ".env"
    if env_path.exists():
        print(f"ℹ️  {env_path.name} already exists. Skipping creation.")
        return
    
    # Use the detailed template provided by the user
    env_path.write_text(ENV_TEMPLATE_CONTENT, encoding="utf-8")
    print(f"\n✅ Created {env_path.name} from template.")
    print("   👉 ACTION REQUIRED: Open .env and add your DISCORD_TOKEN and CLIENT_SECRET.")

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Provision TomCat locally")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--cpu", action="store_true", help="force CPU-only Torch wheels")
    group.add_argument("--gpu", action="store_true", help="force CUDA 12.1 Torch wheels")
    parser.add_argument("--python", type=Path, help="use a specific Python interpreter")
    parser.add_argument("--reinstall", action="store_true", help="force reinstallation")
    parser.add_argument("--resume-model", action="store_true", help="resume DeBERTa download/test")
    parser.add_argument("--skip-model", action="store_true", help="skip DeBERTa download")
    parser.add_argument("--clean-hf-cache", action="store_true", help="delete local HF caches")
    return parser.parse_args()

def main() -> None:
    args = _parse_args()
    python_exe = args.python.resolve() if args.python else Path(sys.executable)
    
    _print_header("TomCat Installer")
    print(f"Root: {ROOT}")
    print(f"Python: {python_exe}")

    if sys.version_info >= (3, 13):
        print(f"\n❌ CRITICAL ERROR: You are using Python {sys.version_info.major}.{sys.version_info.minor}.")
        print("   Please UNINSTALL Python 3.13 and install Python 3.11.")
        import time
        time.sleep(3) 

    _ensure_repo()
    
    # 1. Cloudflare Binary & Auth
    cf_path = _ensure_cloudflared()
    _ensure_cloudflared_auth(cf_path)

    if args.resume_model:
        if not _venv_python().exists():
            raise InstallError(".venv not found – run the full installer first.")
        if args.clean_hf_cache:
            _clean_hf_cache()
        _ensure_deberta_model()
        _test_model()
        _check_yolo_weights()
        _maybe_create_env_template()
    else:
        _create_or_reuse_venv(python_exe)
        
        # 2. Install Torch FIRST (Critical fix for the hang issue)
        torch_force = "gpu" if args.gpu else "cpu" if args.cpu else None
        _install_torch(torch_force)

        # 3. Then install everything else
        try:
            _install_base_dependencies(force_reinstall=args.reinstall)
        except subprocess.CalledProcessError:
            print("\n⚠️  Dependency install failed. Recreating .venv...")
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
        else:
            print("Skipping DeBERTa download/test as requested")

        _check_yolo_weights()
        _maybe_create_env_template()

    print("\n✨ Installation Complete! ✨")
    print("1. Open '.env' and fill in DISCORD_TOKEN and DISCORD_CLIENT_SECRET.")
    print("2. Launch the bot with: python scripts/start.py\n")

if __name__ == "__main__":
    try:
        main()
    except InstallError as exc:
        print(f"✖ {exc}")
        sys.exit(1)
    except subprocess.CalledProcessError as exc:
        print(f"✖ Command failed with exit code {exc.returncode}: {' '.join(exc.cmd)}")
        sys.exit(exc.returncode)