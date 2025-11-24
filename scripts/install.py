#!/usr/bin/env python3
"""One-stop installer for TomCat.

This script provisions the local environment so the bot can run on a fresh
machine. It is safe to run repeatedly – existing assets (repository checkout,
virtualenv, model weights) are reused when present.

What it does:
  • Creates/updates the project virtual environment
  • Downloads cloudflared binary (Windows/Linux)
  • Installs Python dependencies, picking GPU or CPU Torch wheels automatically
  • Downloads & converts the DeBERTa model to ONNX (if missing)
  • Checks for YOLO weights and warns if missing
  • Runs the model smoke test so failures surface immediately
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Iterable, List


ROOT = Path(__file__).resolve().parent.parent
VENV_DIR = ROOT / ".venv"
WEIGHTS_DIR = ROOT / "weights"
ONNX_PATH = WEIGHTS_DIR / "deberta-v3-small-mnli.onnx"
TOKENIZER_PATH = WEIGHTS_DIR / "deberta-v3-small-mnli.tokenizer.json"

# Proprietary weights that must be manually copied
REQUIRED_WEIGHTS = ["NanoModel.pt", "NanoClassifier.pt"]

# PyTorch configuration - ensuring compatibility with Vision + Audio
TORCH_CPU_SPEC = ["torch==2.3.1", "torchvision==0.18.1"]
TORCH_GPU_SPEC = [
    "--extra-index-url",
    "https://download.pytorch.org/whl/cu121",
    "torch==2.3.1+cu121",
    "torchvision==0.18.1+cu121",
]


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


def _ensure_cloudflared() -> None:
    _print_header("Checking Cloudflare Tunnel Binary")
    
    # Determine URL and filename based on OS
    if os.name == "nt":
        filename = "cloudflared.exe"
        url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
    else:
        filename = "cloudflared"
        # Assuming AMD64 for standard servers/desktops; ARM would need a different URL
        url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"

    target_path = ROOT / filename

    if target_path.exists():
        print(f"✅ {filename} is already present.")
        return

    print(f"Downloading {filename} from {url}...")
    try:
        urllib.request.urlretrieve(url, target_path)
        # On Linux/Mac, we must make it executable
        if os.name != "nt":
            target_path.chmod(0o755)
        print(f"✅ Downloaded {filename} to {target_path}")
    except Exception as exc:
        print(f"⚠️ Failed to download cloudflared: {exc}")
        print("You may need to download it manually to run the UI tunnel.")


def _create_or_reuse_venv(python_exe: Path) -> None:
    if _venv_python().exists():
        print("Virtual environment already present – reusing .venv")
        return
    _print_header("Creating virtual environment")
    _run([str(python_exe), "-m", "venv", str(VENV_DIR)])


def _pip(args: Iterable[str]) -> None:
    _run([str(_venv_python()), "-m", "pip", *args])


def _install_base_dependencies(force_reinstall: bool = False) -> None:
    _print_header("Installing Python dependencies")
    _pip(["install", "--upgrade", "pip", "setuptools", "wheel"])

    req_path = ROOT / "requirements.txt"
    base_packages: List[str] = []
    with req_path.open("r", encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            lower = line.lower()
            # We handle Torch separately to manage CUDA/CPU variants explicitly
            if "torch" in lower or lower.startswith("--extra-index-url"):
                continue
            base_packages.append(line)

    if base_packages:
        args = ["install"]
        if force_reinstall:
            args.extend(["--upgrade", "--force-reinstall"])
        args.extend(base_packages)
        _pip(args)


def _detect_cuda() -> bool:
    _print_header("Hardware Detection")
    # Check for nvidia-smi
    if shutil.which("nvidia-smi"):
        print("✅ NVIDIA GPU detected via nvidia-smi.")
        return True
    
    # Fallback check on Windows
    if os.name == 'nt':
        try:
            # Simple check if nvml.dll is loadable might be overkill, 
            # nvidia-smi is the standard indicator.
            pass 
        except Exception:
            pass

    print("ℹ️ No NVIDIA GPU detected – defaulting to CPU wheels.")
    return False


def _torch_installed() -> bool:
    check = subprocess.run(
        [str(_venv_python()), "-c", "import torch, torchvision"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return check.returncode == 0


def _install_torch(force: str | None = None) -> None:
    if _torch_installed():
        print("PyTorch already installed – skipping wheel install")
        return

    wants_gpu: bool
    if force == "gpu":
        wants_gpu = True
    elif force == "cpu":
        wants_gpu = False
    else:
        wants_gpu = _detect_cuda()

    spec = TORCH_GPU_SPEC if wants_gpu else TORCH_CPU_SPEC
    cmd = ["install", *spec]
    
    _print_header(f"Installing PyTorch ({'GPU/CUDA 12.1' if wants_gpu else 'CPU'})")
    try:
        _pip(cmd)
        return
    except subprocess.CalledProcessError:
        if wants_gpu:
            print("⚠️ GPU wheel install failed – retrying with CPU wheels as fallback...")
            _pip(["install", *TORCH_CPU_SPEC])
            return
        raise


def _ensure_extra_models() -> None:
    # Packages needed for ONNX export but not strictly for runtime inference
    _pip([
        "install",
        "huggingface_hub>=0.35.1,<0.36",
        "transformers==4.43.3",
        "safetensors>=0.4.4",
        "sentencepiece>=0.1.99",
        "onnx>=1.16.2,<1.17",
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
    """Checks for the existence of custom YOLO weights."""
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
        print("The bot may fail to perform Visual Identification without them.")


def _test_model() -> None:
    _print_header("Validating ONNX model")
    _run([str(_venv_python()), "scripts/test_model.py"], cwd=ROOT)


def _maybe_create_env_template() -> None:
    env_path = ROOT / ".env"
    if env_path.exists():
        return
    template = (
        "# Fill in your Discord token and channel IDs before starting TomCat\n"
        "DISCORD_TOKEN=replace_me\n"
        "BOT_NAME=TomCat\n"
        "COMMAND_PREFIX=TomCat,\n"
        "# CH_FEEDING_TEAM=\n"
        "# GOOGLE_SERVICE_ACCOUNT_JSON=credentials/service_account.json\n"
    )
    env_path.write_text(template, encoding="utf-8")
    print("\n✅ Created placeholder .env – update it with real credentials before starting.")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Provision TomCat locally")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--cpu", action="store_true", help="force CPU-only Torch wheels")
    group.add_argument("--gpu", action="store_true", help="force CUDA 12.1 Torch wheels")
    parser.add_argument(
        "--python",
        type=Path,
        help="use a specific Python interpreter to create the virtualenv",
    )
    parser.add_argument(
        "--reinstall",
        action="store_true",
        help="force reinstallation of base dependencies",
    )
    parser.add_argument(
        "--resume-model",
        action="store_true",
        help="skip dependency install and rerun only the DeBERTa download/test",
    )
    parser.add_argument(
        "--skip-model",
        action="store_true",
        help="skip the DeBERTa download + test stage",
    )
    parser.add_argument(
        "--clean-hf-cache",
        action="store_true",
        help="delete local HuggingFace caches before downloading models",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    python_exe = args.python.resolve() if args.python else Path(sys.executable)
    _print_header("TomCat Installer")
    print(f"Root: {ROOT}")
    print(f"Python: {python_exe}")

    _ensure_repo()
    
    # 1. Download External Binaries (Cloudflare)
    _ensure_cloudflared()

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
        # 2. Setup Venv & Pip
        _create_or_reuse_venv(python_exe)
        _install_base_dependencies(force_reinstall=args.reinstall)

        # 3. Install Torch (Smart Detect)
        torch_force = "gpu" if args.gpu else "cpu" if args.cpu else None
        _install_torch(torch_force)

        # 4. Download/Convert Models
        if not args.skip_model:
            if args.clean_hf_cache:
                _clean_hf_cache()
            _ensure_deberta_model()
            _test_model()
        else:
            print("Skipping DeBERTa download/test as requested")

        # 5. Final Checks
        _check_yolo_weights()
        _maybe_create_env_template()

    print("\n✨ Installation Complete! ✨")
    print("1. Update .env with your credentials.")
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