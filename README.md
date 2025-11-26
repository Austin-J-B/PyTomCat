# TomCat VI

TomCat is the Campus Cat Coalition’s Discord automation bot. The bot checks feeding,
documents dues and finances, runs computer vision tasks, and keeps large amounts
of data stored and organized for each of the campus cats.
The codebase is built around our 'Intent_router' to keep things 'modular', which
builds off of the problems with the previous 'TomCat v5.6' javascript bot.
Beyond that, part of what the 'TomCat VI' update relies on is utilizing logs and
memory, which gives it the ability to keep track of previous messages and better
fit around normal human language quirks such as sending a picture and text in 
separate messages. This gives extra contextual understanding that creates the 
desired feeling of 'intelligence'.

---

## Main Funcitons

- **Intent-driven sorting** – every message is normalized, evaluated for
  wake words/mentions, matched through aliases/fuzzy/NLP, and routed to the
  appropriate handler.
- **Feeding coordination** – logs stations as fed in the CCC's google sheet,
  tracks volunteer substitution requests, posts nightly 8 PM feeding reminders,
  and keeps uses the github page website as a user interface.
- **Finance automation** – harvests Gmail payment notifications, separates dues
  from other income/expenses, inputs financial data to Google Sheets, and uses
  the logs as a way to understand what type of income or expense any given
  payment is.
- **Cat profiles & photos** – serves profile embeds, random photos, and uses
  the computer vision capability to auto-crop 'Show me' images. On top of that,
  the bot maintains a local cache of images so follow-up requests get quick responses.
- **Computer vision** – detect/crop/identify workflows using Ultralytics YOLOv8 and
  a lightweight classifier. Currently still in development.
- **Spam mitigation** – regex heuristics, fuzzy phrase matching, and an optional
  DeBERTa MNLI model; moderators can ban straight from the alert reaction.
- **Audit trail** – human-readable daily logs plus machine NDJSON for every
  message, edit, reaction, role change, health check, and financial event.

---

## Requirements & Dependencies

`requirements.txt` targets CUDA 12.1 builds suitable for Nvidia GPUs. Highlights:

- **Discord stack:** `discord.py`, `aiohttp`, `python-dotenv`.
- **Google APIs:** `gspread`, `google-api-python-client`, `google-auth-*`.
- **NLP:** `onnxruntime`, `tokenizers` (for zero-shot MNLI + spam entailment).
- **CV:** `ultralytics`, `opencv-python-headless`, `torch`, `torchvision` with
  CUDA 12.1 wheels.
- **Parsing:** `numpy`, `Pillow`, `rapidfuzz`, `beautifulsoup4`, `requests`.

The installer script automatically chooses GPU or CPU wheels for PyTorch based
on whether `nvidia-smi` is available (override with `--gpu` or `--cpu`).

---

## Prerequisites

- Python **3.11** (recommended) or 3.10. Python 3.12 works with the pinned
  `tokenizers`/PyTorch builds, but 3.11 has the broadest wheel coverage.
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
   The installer is idempotent – run it any time to refresh dependencies. It will:
   - Reuse or create `.venv`
   - Install/upgrade core Python dependencies
   - Install PyTorch with CUDA 12.1 wheels when an NVIDIA GPU is detected (fallback to CPU otherwise)
   - Download & convert the DeBERTa MNLI model to ONNX if the files are missing
   - Smoke test the ONNX model and create a placeholder `.env` when none exists
   
   Optional flags:
   ```bash
   python scripts/install.py --cpu           # force CPU-only wheels
   python scripts/install.py --gpu           # force CUDA wheels even if nvidia-smi is absent
   python scripts/install.py --resume-model  # rerun only the DeBERTa download/test step
   python scripts/install.py --clean-hf-cache --resume-model  # clear HF cache then redo the model stage
   ```

3. **Add YOLO weights**
   
   Place the following files in the `weights/` directory:
   - `NanoModel.pt` - YOLO detector for CV detection
   - `NanoClassifier.pt` - Cat classifier head
   
   These files must be copied from an existing deployment or your training artifacts.

4. **Prepare configuration**
   - Copy `.env` (or create it) and fill in:
     - `DISCORD_TOKEN`, `BOT_NAME`, `COMMAND_PREFIX`
     - Channel IDs (`CH_FEEDING_TEAM`, `CH_TOMCAT_SANDBOX`, `CH_LOGGING`, etc.)
     - Google spreadsheet IDs (`SHEET_MEGASHEET_ID`, `SHEET_CATABASE_ID`, …)
     - Gmail OAuth paths (credentials/token JSON)
     - Feeding schedule + user ID maps in `tomcat/config.py`
   - Share your Google sheets with the service account email in
     `GOOGLE_SERVICE_ACCOUNT_JSON`.

6. **Optional: configure Ultralytics cache**
   ```bash
   mkdir -p .ultra
   export YOLO_CONFIG_DIR=$PWD/.ultra  # or set in PowerShell: $env:YOLO_CONFIG_DIR = "$pwd\.ultra"
   ```

7. **Run TomCat**
   ```bash
   python scripts/start.py
   ```

---

## Configuration Cheat Sheet (`.env`)

The `.env` file is heavily commented in-repo. Key groups:

- **Gmail** – enable/disable ingestion, OAuth client/token paths, local OAuth redirect port.
- **Dues** – enable dues automation, accepted amounts, email look-back window,
  scanning limits, NLP toggle, membership cache TTL.
- **Discord** – bot/user IDs and command prefix (also see channel IDs and admin IDs).
- **Caches** – cat profile and alias TTLs.

See `tomcat/config.py` for additional defaults (channels, roles, feeding schedule).

---

## Operations & Logs

- **Human log**: `logs/human/YYYY-MM-DD.log` – timeline of messages, intents,
  feeding actions, finance results, spam alerts, etc.
- **Machine log**: `logs/machine/YYYY-MM/YYYY-MM-DD.ndjson` – structured events
  ready for downstream analytics.
- **Subs log**: `logs/subs/YYYY/YYYY-MM.jsonl` – per-month substitution requests.
- **Finance index**: `logs/finance/index.jsonl` – prevents duplicate processing.

Schedulers:
- Gmail logging every ~4 hours (`start_gmail_logging_scheduler`).
- Feeding 8 PM reminder task (`start_feeding_scheduler`).
- Profile cache refresh and show-photo warmup tasks.

Use these logs to verify automation on first boot. For testing, run manual
commands such as `TomCat, log the past 5 emails` or `TomCat, manual 8pm update`.

---

## Development Tips

- Keep business logic isolated inside handlers/services; the router should only
  orchestrate detection and dispatch.
- Whenever you add Sheets interaction, route through `sheets_client()` so the
  service account session stays cached.
- Extend aliases via `tomcat/aliases.py` for deterministic matches before
  leaning on NLP. Add fuzzy thresholds carefully to avoid cross-cat confusion.
- Honor silent mode via `safe_send`; never call `channel.send` directly.
- Run `python3 -m py_compile $(git ls-files '*.py')` before committing.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Bot silent everywhere | Check `.env` `DISCORD_TOKEN`, ensure silent mode is not enabled (`TomCat, silent mode off`). |
| Gmail auth pending | Trigger `TomCat, check the last email`; follow the OAuth link and respond with the code. |
| Sheets 403/worksheet missing | Share sheet with service account; verify tab names in `CHANNEL_SHEET_MAP`. |
| CV requests fail | Make sure `weights/NanoModel.pt` & `weights/NanoClassifier.pt` exist and the GPU drivers are installed. |
| NLP fallback disabled | Provide the DeBERTa ONNX/tokenizer pair in `weights/` or set `DUES_NLP_ENABLED=false`. |
| Torch install fails | Update NVIDIA drivers; if still failing, fall back to CPU wheels (edit `requirements.txt`). |

---


