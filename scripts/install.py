#!/usr/bin/env python3
"""One-stop installer for TomCat.

This script provisions the local environment so the bot can run on a fresh
machine. It is safe to run repeatedly – existing assets (repository checkout,
virtualenv, model weights) are reused when present.

What it does:
  • Creates/updates the project virtual environment
  • Installs Python dependencies, picking GPU or CPU Torch wheels automatically
  • Downloads & converts the DeBERTa model to ONNX (if missing)
  • Runs the model smoke test so failures surface immediately

Example:
    python scripts/install.py            # auto-detect GPU support
    python scripts/install.py --cpu      # force CPU-only install

The script assumes it is executed from inside the repository root (the same
directory that contains requirements.txt)."""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable, List


ROOT = Path(__file__).resolve().parent.parent
VENV_DIR = ROOT / ".venv"
WEIGHTS_DIR = ROOT / "weights"
ONNX_PATH = WEIGHTS_DIR / "deberta-v3-small-mnli.onnx"
TOKENIZER_PATH = WEIGHTS_DIR / "deberta-v3-small-mnli.tokenizer.json"

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
            "requirements.txt not found. Run this script from the repository root."\
        )


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
    if shutil.which("nvidia-smi"):
        print("Detected NVIDIA GPU via nvidia-smi")
        return True
    print("No NVIDIA GPU detected – defaulting to CPU wheels")
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
    try:
        _pip(cmd)
        return
    except subprocess.CalledProcessError:
        if wants_gpu:
            print("GPU wheel install failed – retrying with CPU wheels")
            _pip(["install", *TORCH_CPU_SPEC])
            return
        raise


def _ensure_extra_models() -> None:
    # These lightweight packages are not part of requirements to avoid inflating
    # runtime footprint when the NLP backstop is unused, but we need them for the
    # ONNX export step.
    _pip(["install", "huggingface_hub<0.34", "transformers==4.43.3", "safetensors>=0.4.4"])


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
        print("DeBERTa ONNX model already present – skipping conversion")
        return
    _print_header("Downloading & converting DeBERTa (MNLI) ONNX model")
    _ensure_extra_models()
    _run([str(_venv_python()), "scripts/convert_model.py"], cwd=ROOT)
    _cleanup_tokenizer_artifacts()


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
    print("Created placeholder .env – update it with real credentials")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Provision TomCat locally")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--cpu", action="store_true", help="force CPU-only Torch wheels")
    group.add_argument("--gpu", action="store_true", help="force CUDA 12.1 Torch wheels")
    parser.add_argument(
        "--python",
        type=Path,
        help="use a specific Python interpreter to create the virtualenv (defaults to the current interpreter)",
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
        help="provision dependencies but skip the DeBERTa download + test stage",
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
    print(f"Repository root: {ROOT}")
    print(f"Using Python interpreter: {python_exe}")

    _ensure_repo()
    if args.resume_model:
        if not _venv_python().exists():
            raise InstallError(".venv not found – run the full installer first.")
        if args.clean_hf_cache:
            _clean_hf_cache()
        _ensure_deberta_model()
        _test_model()
        _maybe_create_env_template()
    else:
        _create_or_reuse_venv(python_exe)
        _install_base_dependencies(force_reinstall=args.reinstall)

        torch_force = "gpu" if args.gpu else "cpu" if args.cpu else None
        _install_torch(torch_force)

        if not args.skip_model:
            if args.clean_hf_cache:
                _clean_hf_cache()
            _ensure_deberta_model()
            _test_model()
        else:
            print("Skipping DeBERTa download/test as requested")
        _maybe_create_env_template()

    print("\nAll done! Launch the bot with:\n  python scripts/start.py\n")


if __name__ == "__main__":
    try:
        main()
    except InstallError as exc:
        print(f"✖ {exc}")
        sys.exit(1)
    except subprocess.CalledProcessError as exc:
        print(f"✖ Command failed with exit code {exc.returncode}: {' '.join(exc.cmd)}")
        sys.exit(exc.returncode)
