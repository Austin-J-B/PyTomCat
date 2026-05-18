# TomCat VI

TomCat is the Campus Cat Coalition's Discord automation bot. The bot checks feeding,
documents dues and finances, runs computer vision tasks, and keeps large amounts
of data stored and organized for each of the campus cats.
The codebase is built around our 'Intent_router' to keep things modular, which
builds off of the problems with the previous 'TomCat v5.6' javascript bot.
Beyond that, part of what the 'TomCat VI' update relies on is utilizing logs and
memory, which gives it the ability to keep track of previous messages and better
fit around normal human language quirks such as sending a picture and text in 
separate messages. This gives extra contextual understanding that creates the 
desired feeling of 'intelligence'.

This bot is a continued work in progress! Much of the 'financial logging and analysis' 
has been completed in the fall of 2025, but the ReID and automated-training tasks for
the Computer Vision system are still in development.

---

## Main Funcitons

- **Intent-driven sorting**: every message is normalized, evaluated for
  wake words/mentions, matched through aliases/fuzzy/NLP, and routed to the
  appropriate handler.
- **Feeding coordination**: logs stations as fed in the CCC's google sheet,
  tracks volunteer substitution requests, posts nightly 8PM feeding reminders,
  and keeps uses the github page website as a user interface.
- **Finance automation**: harvests Gmail payment notifications, separates dues
  from other income/expenses, inputs financial data to Google Sheets, and uses
  the logs as a way to understand what type of income or expense any given
  payment is.
- **Cat profiles & photos**: serves profile embeds, random photos, and uses
  the computer vision capability to auto-crop 'Show me' images. On top of that,
  the bot maintains a local cache of images so follow-up requests get quick responses.
- **Computer vision**: detect/crop/identify workflows using Ultralytics YOLO12 and
  a lightweight classifier. Currently still in development.
- **Spam mitigation**: regex heuristics and fuzzy phrase matching; moderators
  can ban straight from the alert reaction.
- **Audit trail**: human-readable daily logs plus machine NDJSON for every
  message, edit, reaction, role change, health check, and financial event.

---

## Requirements & Dependencies

`requirements.txt` targets CUDA 12.1 builds suitable for Nvidia GPUs. Highlights:

- **Discord stack:** `discord.py`, `aiohttp`, `python-dotenv`.
- **Google APIs:** `gspread`, `google-api-python-client`, `google-auth-*`.
- **NLP:** deterministic rule-based intent router (no LLM). Earlier versions
  used `llama-cpp-python` with SmolLM2 for ambiguous-query routing; that path
  was retired since rules cover the actually-used command surface.
- **CV:** `ultralytics`, `opencv-python-headless`, `torch`, `torchvision` with
  CUDA 12.1 wheels.
- **Parsing:** `numpy`, `Pillow`, `rapidfuzz`, `beautifulsoup4`, `requests`.

The installer script automatically chooses GPU or CPU wheels for PyTorch based
on whether `nvidia-smi` is available (override with `--gpu` or `--cpu`).

---

## Prerequisites

- Python **3.11** (recommended) or 3.12.
- NVIDIA drivers supporting CUDA 12.1 (Windows) or CUDA toolkit/driver available
  under WSL
- Google Cloud project with a Gmail API OAuth client + Google Sheets service
  account; share your Sheets with the service account email.
- Discord bot application created in the developer portal with `MESSAGE CONTENT`
  intent enabled.

---

## Installation

1. **Clone the repository**
   ```bash
   git clone https://github.com/Austin-J-B/PyTomCat.git
   cd PyTomCat
   ```

2. **Provision the environment**
   ```bash
   python scripts/install.py
   ```
   The installer is idempotent â€“ run it any time to refresh dependencies. It will:
   - Reuse or create `.venv`
   - Install/upgrade core Python dependencies
   - Install PyTorch with CUDA 12.1 wheels when an NVIDIA GPU is detected (fallback to CPU otherwise)
   - Install the local LLM runtime and download the default GGUF model when missing
   - Create a placeholder `.env` when none exists
   
   Optional flags:
   ```bash
   python scripts/install.py --cpu           #force CPU-only wheels
   python scripts/install.py --gpu           #force CUDA wheels even if nvidia-smi is absent
   python scripts/install.py --resume-model  #rerun only the local-model/runtime step
   python scripts/install.py --clean-hf-cache --resume-model  #clear HF cache then redo the model stage
   ```

3. **Add Vision weights**
   
   Place the following files in the `weights/` directory:
   - `984_917_yolo12s.pt` - YOLOv12 detector
   - `R4_cat_DINOv3_encoder.pth` - DINOv3 encoder
   - `R4.5.X_cat_DINOv3_gallery.pt` - Embedding gallery for classification
   
   These files must be copied from an existing deployment or training artifacts.

4. **Prepare configuration**
   - Copy `.env` (or create it) and fill in:
     - `DISCORD_TOKEN`, `BOT_USER_ID`, `COMMAND_PREFIX`
     - Channel IDs (`CH_FEEDING_TEAM`, `CH_TOMCAT_SANDBOX`, `CH_LOGGING`, etc.)
     - Google spreadsheet IDs (`SHEET_MEGASHEET_ID`, `SHEET_CATABASE_ID`, etc.)
     - Gmail OAuth paths (credentials/token JSON)
     - Feeding schedule + user ID maps in `tomcat/config.py`
   - Share your Google sheets with the service account email in `GOOGLE_SERVICE_ACCOUNT_JSON`.

5. **Optional: configure Ultralytics cache**
   - `scripts/start.py` now sets `YOLO_CONFIG_DIR` to `./.ultra` automatically.
   - If you want to override it, set `YOLO_CONFIG_DIR` yourself:
   ```bash
   mkdir -p .ultra
   export YOLO_CONFIG_DIR=$PWD/.ultra  #or set in PowerShell: $env:YOLO_CONFIG_DIR = "$pwd\.ultra"
   ```

6. **Run TomCat**
   ```bash
   python scripts/start.py
   ```

---
