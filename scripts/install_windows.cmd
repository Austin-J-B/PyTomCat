@echo off
REM PyTomCat Windows install helper (cmd.exe)
REM Usage: open cmd.exe in the repo root and run: scripts\install_windows.cmd

echo === PyTomCat Windows installer ===

:: Check Python availability
where python >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo Python not found in PATH. Please install Python 3.11 or 3.10 and add it to PATH.
    goto :end
)

:: Create virtual environment
if not exist ".venv\Scripts\activate" (
    echo Creating virtual environment in .venv ...
    python -m venv .venv
) else (
    echo Virtual environment already exists.
)

echo To activate the venv in this cmd session, run:
echo    .\.venv\Scripts\activate

echo Upgrading pip ...
.\.venv\Scripts\python.exe -m pip install --upgrade pip setuptools wheel

echo Installing core dependencies from requirements.txt (will use pip inside venv)...
.\.venv\Scripts\pip.exe install -r requirements.txt
if %ERRORLEVEL% neq 0 (
    echo.
    echo WARNING: pip install returned a non-zero exit code. Torch installation often fails due to CUDA requirements.
    echo If you have a CUDA 12.1 GPU and want GPU support, re-run the following (inside the venv):
    echo    .\.venv\Scripts\pip.exe install --extra-index-url https://download.pytorch.org/whl/cu121 torch==2.3.1+cu121 torchvision==0.18.1+cu121
    echo For CPU-only, run (inside the venv):
    echo    .\.venv\Scripts\pip.exe install torch==2.3.1 torchvision==0.18.1
    echo See README.md for more details.
) else (
    echo Dependencies installed successfully.
)

:: Create weights directory if missing
if not exist weights (
    mkdir weights
    echo Created weights/ directory. Place your NanoModel.pt and NanoClassifier.pt here, and add the DeBERTa ONNX/tokenizer files if using NLP.
) else (
    echo weights/ directory already present.
)

:: .env guidance file copy (safe to overwrite?)
if not exist .env (
    echo Creating example .env from template (.env is required for runtime configuration)...
    echo DISCORD_TOKEN=your_token_here> .env
    echo BOT_NAME=TomCat>> .env
    echo COMMAND_PREFIX=TomCat,>> .env
    echo # Add channel IDs and Google credentials paths as needed >> .env
) else (
    echo .env already exists; edit it to add required credentials.
)

:info
echo.
echo Installation helper finished. Next steps:
echo 1) Activate venv in this cmd session:
echo    .\.venv\Scripts\activate
echo 2) If pip install failed or you want GPU PyTorch, run one of these (inside venv):
echo    :: For CUDA 12.1 (GPU)
echo    .\.venv\Scripts\pip.exe install --extra-index-url https://download.pytorch.org/whl/cu121 torch==2.3.1+cu121 torchvision==0.18.1+cu121
echo    :: For CPU-only
echo    .\.venv\Scripts\pip.exe install torch==2.3.1 torchvision==0.18.1
echo 3) Download weights into weights/ (see README). Example DeBERTa via huggingface_hub CLI:
echo    .\.venv\Scripts\pip.exe install huggingface_hub
echo    .\.venv\Scripts\huggingface-cli.exe download microsoft/deberta-v3-small --include "*.onnx" "*.tokenizer.json" --local-dir weights --local-dir-use-symlinks False
echo    Rename the files to: weights\deberta-v3-small-mnli.onnx and weights\deberta-v3-small-mnli.tokenizer.json
echo 4) Run TomCat:
echo    .\.venv\Scripts\activate
echo    python -m tomcat.main

:end
echo.
