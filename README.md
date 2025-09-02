TomCat VI — Discord Bot for Campus Cat Coalition
================================================

TomCat is a Discord bot that coordinates feeding, cat profiles, computer vision, and logging for our server. This repo is the Python rewrite ("TomCat VI") with a strong focus on: clean intent routing, conservative NLP, silent-by-default automation, and rich file logs you can treat as the bot’s memory.

Overview
--------
- Router-first design: `tomcat/intent_router.py` is the brain. Every message is normalized, addressed detection runs, then rules → aliases/fuzzy → optional NLP backstops decide the intent. Dispatch fans out to slim handlers.
- Silent-by-default: TomCat only speaks when addressed (wake word, @mention, or DM), except for allowed channels like `#feeding-team` where specific flows (e.g., subs/feeds) can run quietly.
- File-first logs: Human-readable daily log + machine NDJSON. Health checks at boot, rich audit trails for message edits/deletes, member joins/leaves, role changes, reactions, intent decisions, spam, etc.
- Safety & robustness: spam gate with heuristics + fuzzy + optional DeBERTa; invite attribution on joins; safe_send wrapper honors global silent mode.
- CV integration: show/crop/detect/identify with YOLO + classifier, on single-cat crops only. GPU is supported via CUDA PyTorch.

Directory Map
-------------
- `tomcat/main.py` — Discord client, lifecycle, logging, spam checks, health checks, and plumbing.
- `tomcat/intent_router.py` — Intent detection + dispatch. Handles wake/mention/DM, CV pairing windows, feeding/subs routing, and admin commands.
- `tomcat/handlers/` — Slim, single-purpose handlers.
  - `cats.py` — Profiles and “show me a photo of …” with optional single-cat crop and a “Show me another” button that edits in place.
  - `feeding.py` — Feed updates (sheet mark), sub requests/accepts (log-only), 8pm alerts, schedule and user map, and the manual 8pm preview.
  - `vision.py` — CV: detect/crop/identify using YOLO + classifier; safe JPEG utilities.
  - `misc.py` — Small fun or utility triggers, profile builder, image-intake to Sheets.
  - `dues.py`, `admin.py` — Dues stub and admin toggles (e.g., silent mode).
- `tomcat/aliases.py` — Cat + station names, nicknames, and deterministic resolvers (whole word and unambiguous partials — stopwords excluded for stations).
- `tomcat/spam.py` — Heuristics + fuzzy + optional DeBERTa spam scorer with trust exceptions.
- `tomcat/nlp/model.py` — Optional ONNX wrapper for zero-shot intent/entity scoring and a spam entailment scorer.
- `tomcat/services/` — Google Sheets clients and CatDatabase/RecentPics helpers.
- `tomcat/vision/` — CV engine: YOLO detector, classifier, drawing and cropping.
- `logs/` — Output logs: human (readable) + machine (NDJSON), plus `logs/subs/subs.jsonl` for sub requests.

Philosophy
----------
1) Router as the brain
   - Normalize text and context → detect addressing (wake word/mention/DM) → rules/aliases/fuzzy → optional NLP backstop.
   - Confidence-first: we err on the side of silence. NLP is used only when addressed or in specific channels and above configured thresholds.

2) Silent automation
   - Feeding image intake and subs logging: silent. 8pm alerts go to a known channel. Manual previews are admin-only.

3) Logs as “memory”
   - TomCat writes everything important to file logs: messages, edits, deletes, joins/leaves, roles, reactions, intents, health checks, spam decisions. This enables auditing and future analytics without a database.

Core Features
-------------
- Cats & Photos
  - “TomCat, who is Microwave” — profile embed.
  - “TomCat, show me Microwave” — random photo (auto-crop if exactly one cat detected). “Show me another” edits in place.

- Computer Vision
  - “TomCat, identify/detect/crop” — pairs with an attached image, a reply-to image, or your most recent image (short window). If no image yet, sets a pending and fires when you upload.
  - Crops only when a single cat is detected; otherwise returns the original.

- Feeding Team
  - Feed updates: verbs (“fed/filled/topped off …”) mark the sheet’s checkbox. Multi-station updates supported.
  - Subs: “can someone cover …” in feeding channels logs a sub request silently. If no station is named, TomCat infers stations from the requester’s schedule for the requested dates. Accepts are recognized with broad but safe phrasing (“I can”, “I’ll take it”, “I’ve got it”, “sure”).
  - Status: “TomCat, who has/hasn’t been fed today?” returns Fed/Unfed lists (no pings).
  - 8pm alert: pings accepted subs for today; otherwise scheduled feeders; “Unassigned.” when none. Admin-only “TomCat, manual 8pm update” posts a dry run (no pings).

- Image Sourcing
  - Channel → Sheets intake: maps channels to tab names. We try Vision first, then Catabase (e.g., “TCBPicsInput”). Writes rows as `[URL, @username, timestampZ]`.

- Spam Protection
  - Heuristics (phones, emails, “first come first serve”, “DM me if interested”, free giveaways), fuzzy phrases, and a zero-shot spam entailment scorer.
  - Trust rules: skip detection for accounts older than N days or those with trusted roles (configured).
  - Action: deletes the message (best effort), logs a Spam line (reason and decision), and posts a mod alert in CH_LOGGING mentioning SPAM_ALERT_USER_ID (or first admin).

- Invite Tracking & Admin Logs
  - Seeds invite usage cache on startup; on member join, diffs invite usage and logs invite code and inviter. Logs reactions, role changes, edits, deletes in human-friendly lines.

Configuration (.env)
--------------------
- Discord
  - DISCORD_TOKEN=…
  - COMMAND_PREFIX=!
  - TOMCAT_WAKE=TomCat
  - TIMEZONE=America/Chicago
  - ADMIN_IDS=comma,separated,ids
  - CH_FEEDING_TEAM=…, CH_TOMCAT_SANDBOX=…
  - CH_PICTURES_OF_CATS=…, CH_REPORT_NEW_CATS=…
  - CH_LOGGING=…
  - CHANNEL_SHEET_MAP="CH_PICTURES_OF_CATS:TCBPicsInput,CH_REPORT_NEW_CATS:TCBPicsInput,…"
  - allowed_feeding_channel_ids=[CH_FEEDING_TEAM, CH_TOMCAT_SANDBOX]

- Google Sheets
  - GOOGLE_SERVICE_ACCOUNT_JSON=./credentials/service_account.json
  - SHEET_CATABASE_ID=…
  - SHEET_VISION_ID=…  (FeedingStationChecklist lives here)

- NLP/CV
  - NLP_MODEL_PATH=weights/deberta-v3-small-mnli.onnx
  - NLP_TOKENIZER_PATH=weights/deberta-v3-small-mnli.tokenizer.json
  - CV_DETECT_WEIGHTS=weights/NanoModel.pt
  - CV_CLASSIFY_WEIGHTS=weights/NanoClassifier.pt
  - CV_TIMEOUT_MS=2000  (budget for quick one-shot crop on “show me”)

- Feeding schedule
  - user_id_map = {"Chris": 6244…, …}
  - feeding_schedule = { "Business": ["Chris","Chris","Chris","Megan","Megan","Megan","Ben"], … }  # Sun..Sat by name

- Spam
  - SPAM_MIN_ACCOUNT_DAYS=30
  - SPAM_ALERT_USER_ID=<your id>
  - trusted_role_names in config.py (defaults include “due paying”, “member”, “officer”)

Running the Bot
---------------

Prereqs
- Python 3.11/3.12
- NVIDIA GPU with CUDA 12.1 drivers for CV acceleration
- A Google service account JSON with access to your sheets

Windows (PowerShell / VS Code Terminal)
1) Clone repo and cd into it.
2) Create venv and activate:
   - `python -m venv .venv`
   - `.\.venv\Scripts\Activate.ps1`
3) Install requirements (includes CUDA12.1 torch wheels):
   - `pip install -U pip`
   - `pip install -r requirements.txt`
4) Set up .env with your tokens, sheet IDs, and channel IDs.
5) (Optional) Set YOLO config dir to avoid warnings:
   - `$env:YOLO_CONFIG_DIR = "$pwd\.ultra"; mkdir .ultra` (if not exists)
6) Run:
   - `python -m tomcat.main`

WSL Ubuntu (bash)
1) `python3 -m venv .venv && source .venv/bin/activate`
2) `pip install -U pip`
3) `pip install -r requirements.txt`
4) Ensure `.env` is populated and `credentials/service_account.json` exists.
5) (Optional) `export YOLO_CONFIG_DIR=$PWD/.ultra && mkdir -p .ultra`
6) `python -m tomcat.main`

Troubleshooting
---------------
- Missing Pillow/Ultralytics/Torch: re-run `pip install -r requirements.txt`.
- GPU not used: verify `nvidia-smi` shows your GPU in WSL; ensure CUDA12.1 torch wheels installed (requirements.txt includes `--extra-index-url` for cu121), and that the driver is current.
- Sheets 403/worksheet missing: confirm the service account email has been shared to the sheet. Check `CHANNEL_SHEET_MAP` tab names exist (we try Vision first, then Catabase).
- No responses: check your wake word (“TomCat,”) or use an @mention. Silent mode suppresses replies globally; disable if needed.
- Logs: tail `logs/human/*.log` and `logs/machine/*.ndjson` while testing.

Contributing
------------
- Keep handlers small and composable. If an intent grows, anchor it in the router and push the work into a handler.
- Prefer silent logging to UI. Use file logs as the source of truth.
- Add or tune aliases and nicknames locally (no sheet dependency) for deterministic behavior; let NLP backstop, not lead.

