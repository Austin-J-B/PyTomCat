# tomcat/config.py
from __future__ import annotations
import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()

def _get_env_list(key: str, sep: str = ",") -> list[str]:
    raw = os.getenv(key, "")
    return [s.strip() for s in raw.split(sep) if s.strip()]

def _get_env_bool(key: str, default: bool = False) -> bool:
    v = os.getenv(key)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}





@dataclass
class Settings:
    # Discord
    discord_token: str = os.getenv("DISCORD_TOKEN", "")
    command_prefix: str = os.getenv("COMMAND_PREFIX", "!")
    bot_name: str = os.getenv("BOT_NAME", "tomcat")
    tomcat_wake: str = os.getenv("TOMCAT_WAKE", os.getenv("BOT_NAME", "tomcat"))
    timezone: str = os.getenv("TIMEZONE", "America/Chicago")

    # Admins
    admin_ids: list[int] = field(default_factory=lambda: [
        int(x) for x in _get_env_list("ADMIN_IDS")
        if x.strip().lstrip("-").isdigit()
    ])
    silent_mode: bool = field(default_factory=lambda: _get_env_bool("SILENT_MODE"))

    # Channels
    ch_due_portal: int | None = int(os.getenv("CH_DUE_PORTAL", "0")) or None
    ch_feeding_team: int | None = int(os.getenv("CH_FEEDING_TEAM", "0")) or None
    ch_pictures_of_cats: int | None = int(os.getenv("CH_PICTURES_OF_CATS", "0")) or None
    ch_report_new_cats: int | None = int(os.getenv("CH_REPORT_NEW_CATS", "0")) or None
    ch_member_names: int | None = int(os.getenv("CH_MEMBER_NAMES", "0")) or None
    ch_logging: int | None = int(os.getenv("CH_LOGGING", "0")) or None
    ch_sandbox: int | None = int(os.getenv("CH_TOMCAT_SANDBOX", "0")) or None

    # Google service account
    google_service_account_json: str = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "credentials/service_account.json")
    # Keep your older name too
    google_sa_json: str = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "credentials/service_account.json")

    # Sheets (support both old and new env names)
    # Old names in your .env: SHEET_CATABASE_ID, SHEET_VISION_ID, SHEET_MEGASHEET_ID
    sheet_catabase_id: str | None = os.getenv("SHEET_CATABASE_ID") or os.getenv("CAT_SPREADSHEET_ID")
    sheet_vision_id: str | None = os.getenv("SHEET_VISION_ID") or os.getenv("AUX_SPREADSHEET_ID")
    sheet_megasheet_id: str | None = os.getenv("SHEET_MEGASHEET_ID")

    # Also keep the new-style names some modules were using
    cat_spreadsheet_id: str | None = os.getenv("CAT_SPREADSHEET_ID") or os.getenv("SHEET_CATABASE_ID")
    aux_spreadsheet_id: str | None = os.getenv("AUX_SPREADSHEET_ID") or os.getenv("SHEET_VISION_ID")

    # Logging
    log_dir: str = os.getenv("LOG_DIR", "./logs")

    # Channels where misc handlers like "meow" are allowed (empty set means everywhere)
    misc_channels: set[int] = field(default_factory=set)

        # ======== CV CONFIG (v5.6 parity; easy to tweak) ========
    # Paths (drop-and-play). Change these when you retrain/move weights.
    cv_detect_weights: str = os.getenv(
        "CV_DETECT_WEIGHTS",
        r"C:\Users\austi\Documents\GitHub\PyTomCat\weights\NanoModel.pt",
    )
    cv_classify_weights: str = os.getenv(
        "CV_CLASSIFY_WEIGHTS",
        r"C:\Users\austi\Documents\GitHub\PyTomCat\weights\NanoClassifier.pt",
    )

    # Optional human-readable class names for the classifier. Leave empty to use Cat{idx}.
    cv_class_names: list[str] = field(default_factory=lambda: [
        "Microwave", "Faye", "Bobbie", "Twix", "Citlali", "Angel", "Winston", "Radar", "Eggs", "Dumpster",
        "Gregory", "Rubber", "Bruno", "Boots", "Princess", "Nefarious", "Eraser", "Eden", "Cassie", "Coronavirus"
    ])

    # Core knobs (kept at your v5.6 values by default)
    cv_conf: float = float(os.getenv("CV_CONF", "0.552"))           # detector confidence
    cv_iou: float = float(os.getenv("CV_IOU", "0.45"))              # NMS IoU
    cv_detect_imgsz: int = int(os.getenv("CV_DETECT_IMGSZ", "640")) # YOLO inference size
    cv_clf_imgsz: int = int(os.getenv("CV_CLF_IMGSZ", "640"))       # classifier input size
    cv_pad_pct: float = float(os.getenv("CV_PAD_PCT", "0.03"))      # crop expansion

    # Safety/limits
    cv_max_image_dim: int = int(os.getenv("CV_MAX_IMAGE_DIM", "4096"))   # 4K cap on longest side
    cv_max_download_mb: int = int(os.getenv("CV_MAX_DOWNLOAD_MB", "16")) # attachment size cap

    # Device/precision
    cv_half: bool = os.getenv("CV_FP16", "1").strip().lower() in {"1","true","yes","on"}

    # Temp folder for downloads (repo-local, not hidden OS temp)
    cv_temp_dir: str = os.getenv("CV_TEMP_DIR", "./temp_images")
    # Auto-crop for "show me" / "who is"
    auto_crop_show_photo: bool = os.getenv("AUTO_CROP_SHOW_PHOTO", "1").strip().lower() in {"1","true","yes","on"}
    # Hard budget for auto-crop work in handlers (ms). If exceeded, show original image.
    cv_timeout_ms: int = int(os.getenv("CV_TIMEOUT_MS", "800"))







settings = Settings()



# Back-compat touches
if not settings.tomcat_wake:
    settings.tomcat_wake = settings.bot_name
# Ensure both sheet_* and *_spreadsheet_id are aligned
if not settings.sheet_catabase_id and settings.cat_spreadsheet_id:
    settings.sheet_catabase_id = settings.cat_spreadsheet_id
if not settings.sheet_vision_id and settings.aux_spreadsheet_id:
    settings.sheet_vision_id = settings.aux_spreadsheet_id
