# tomcat/config.py
"""
Configuration and tuning knobs for TomCat.
"""
from __future__ import annotations
import os
from dataclasses import dataclass, field
from dotenv import load_dotenv
from typing import Dict

load_dotenv()

def _get_env_list(key: str, sep: str = ",") -> list[str]:
    raw = os.getenv(key, "")
    return [s.strip() for s in raw.split(sep) if s.strip()]

def _get_env_bool(key: str, default: bool = False) -> bool:
    v = os.getenv(key)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}

def _parse_channel_list_env(key: str) -> list[int]:
    raw = (os.getenv(key, "") or "").strip()
    if not raw: return []
    if raw.startswith("[") and raw.endswith("]"): raw = raw[1:-1]
    toks = [t.strip() for t in raw.split(",") if t.strip()]
    out: list[int] = []
    for t in toks:
        val = os.getenv(t, t)
        try:
            cid = int(str(val).strip())
            if cid: out.append(cid)
        except Exception: continue
    return out

def _build_channel_sheet_map() -> dict[int, str]:
    raw = os.getenv("CHANNEL_SHEET_MAP", "").strip()
    out: dict[int, str] = {}
    if raw:
        for pair in (p.strip() for p in raw.split(",") if p.strip()):
            if ":" not in pair: continue
            k, tab = (s.strip() for s in pair.split(":", 1))
            chan = os.getenv(k) if not k.isdigit() else k
            if not chan: continue
            try: cid = int(chan)
            except Exception: continue
            if cid and tab: out[cid] = tab
        if out: return out
    # defaults
    def _id(name: str) -> int | None:
        try: return int(os.getenv(name, "0") or 0) or None
        except Exception: return None
    pics = _id("CH_PICTURES_OF_CATS")
    rpt  = _id("CH_REPORT_NEW_CATS")
    if pics: out[pics] = "TCBPicsInput"
    if rpt:  out[rpt]  = "TCBPicsInput"
    return out

@dataclass
class Settings:
    # Discord
    discord_token: str = os.getenv("DISCORD_TOKEN", "")
    discord_client_secret: str = os.getenv("DISCORD_CLIENT_SECRET", "") # Needed for OAuth
    ui_activity_app_id: str = os.getenv("UITEST_ACTIVITY_APP_ID", "") # Activity ID
    
    command_prefix: str = os.getenv("COMMAND_PREFIX", "!")
    bot_name: str = os.getenv("BOT_NAME", "tomcat")
    tomcat_wake: str = os.getenv("TOMCAT_WAKE", os.getenv("BOT_NAME", "tomcat"))
    bot_user_id: int | None = int(os.getenv("BOT_USER_ID", "1341667150066225192") or "0") or None
    bot_dm_id: int | None = int(os.getenv("BOT_DM_ID", "1352882061651873863") or "0") or None
    timezone: str = os.getenv("TIMEZONE", "America/Chicago")
    
    channel_sheet_map: dict[int, str] = field(default_factory=_build_channel_sheet_map)
    dm_image_tab: str = os.getenv("DM_IMAGE_TAB", "TCBPicsInput")
    dm_image_sheet_id: str | None = os.getenv("DM_IMAGE_SHEET_ID") or None
    
    # Admins
    admin_ids: list[int] = field(default_factory=lambda: [
        int(x) for x in _get_env_list("ADMIN_IDS") if x.strip().lstrip("-").isdigit()
    ])
    silent_mode: bool = field(default_factory=lambda: _get_env_bool("SILENT_MODE", False))

    # Channels
    ch_due_portal: int | None = int(os.getenv("CH_DUE_PORTAL", "0")) or None
    ch_feeding_team: int | None = int(os.getenv("CH_FEEDING_TEAM", "0")) or None
    ch_pictures_of_cats: int | None = int(os.getenv("CH_PICTURES_OF_CATS", "0")) or None
    ch_report_new_cats: int | None = int(os.getenv("CH_REPORT_NEW_CATS", "0")) or None
    ch_member_names: int | None = int(os.getenv("CH_MEMBER_NAMES", "0")) or None
    ch_logging: int | None = int(os.getenv("CH_LOGGING", "0")) or None
    ch_sandbox: int | None = int(os.getenv("CH_TOMCAT_SANDBOX", "0")) or None
    
    allowed_feeding_channel_ids: list[int] = field(default_factory=lambda: _parse_channel_list_env("allowed_feeding_channel_ids"))

    # Google / Sheets
    google_service_account_json: str = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "credentials/service_account.json")
    google_sa_json: str = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "credentials/service_account.json")
    sheet_catabase_id: str | None = os.getenv("SHEET_CATABASE_ID") or os.getenv("CAT_SPREADSHEET_ID")
    sheet_vision_id: str | None = os.getenv("SHEET_VISION_ID") or os.getenv("AUX_SPREADSHEET_ID")
    sheet_megasheet_id: str | None = os.getenv("SHEET_MEGASHEET_ID")
    cat_spreadsheet_id: str | None = os.getenv("CAT_SPREADSHEET_ID") or os.getenv("SHEET_CATABASE_ID")
    aux_spreadsheet_id: str | None = os.getenv("AUX_SPREADSHEET_ID") or os.getenv("SHEET_VISION_ID")
    finance_sheet_throttle_sec: float = float(os.getenv("FINANCE_SHEET_THROTTLE_SEC", "0.5") or "0.5")

    # Local Storage
    log_dir: str = os.getenv("LOG_DIR", "./logs")
    schedule_file: str = os.getenv("SCHEDULE_FILE", "data/feeding_schedule.json")

    misc_channels: set[int] = field(default_factory=set)

    # CV / ML
    cv_detect_weights: str = os.getenv("CV_DETECT_WEIGHTS", os.path.join("weights", "NanoModel.pt"))
    cv_classify_weights: str = os.getenv("CV_CLASSIFY_WEIGHTS", os.path.join("weights", "NanoClassifier.pt"))
    cv_class_names: list[str] = field(default_factory=lambda: [
        "Microwave", "Faye", "Bobbie", "Twix", "Citlali", "Angel", "Winston", "Radar", "Eggs", "Dumpster",
        "Gregory", "Rubber", "Bruno", "Boots", "Princess", "Nefarious", "Eraser", "Eden", "Cassie", "Coronavirus"
    ])
    cv_conf: float = float(os.getenv("CV_CONF", "0.552"))
    cv_iou: float = float(os.getenv("CV_IOU", "0.45"))
    cv_detect_imgsz: int = int(os.getenv("CV_DETECT_IMGSZ", "640"))
    cv_clf_imgsz: int = int(os.getenv("CV_CLF_IMGSZ", "640"))
    cv_pad_pct: float = float(os.getenv("CV_PAD_PCT", "0.03"))
    cv_max_image_dim: int = int(os.getenv("CV_MAX_IMAGE_DIM", "10000"))
    cv_max_download_mb: int = int(os.getenv("CV_MAX_DOWNLOAD_MB", "16"))
    cv_half: bool = os.getenv("CV_FP16", "1").strip().lower() in {"1","true","yes","on"}
    cv_temp_dir: str = os.getenv("CV_TEMP_DIR", "./temp_images")
    cv_log_crop: bool = _get_env_bool("CV_LOG_CROP", True)
    auto_crop_show_photo: bool = os.getenv("AUTO_CROP_SHOW_PHOTO", "1").strip().lower() in {"1","true","yes","on"}
    cv_timeout_ms: int = int(os.getenv("CV_TIMEOUT_MS", "6000"))
    cv_lookback_seconds_before: int = int(os.getenv("CV_LOOKBACK_SECONDS_BEFORE", "30"))
    cv_pending_minutes_after: int = int(os.getenv("CV_PENDING_MINUTES_AFTER", "5"))

    # Cache
    show_cache_dir: str = os.getenv("SHOW_CACHE_DIR", "./cache/show_photos")
    show_cache_per_cat: int = int(os.getenv("SHOW_CACHE_PER_CAT", "5") or "5")
    show_cache_prefill_on_boot: bool = _get_env_bool("SHOW_CACHE_PREFILL_ON_BOOT", True)
    show_cache_warm_concurrency: int = int(os.getenv("SHOW_CACHE_WARM_CONCURRENCY", "2") or "2")
    show_cache_warm_limit: int = int(os.getenv("SHOW_CACHE_WARM_LIMIT", "0") or "0")
    show_cache_resize_max_dim: int = int(os.getenv("SHOW_CACHE_RESIZE_MAX_DIM", "0") or "0")
    show_cache_jpeg_quality: int = int(os.getenv("SHOW_CACHE_JPEG_QUALITY", "88") or "88")
    show_sheet_recentpics_ttl_sec: int = int(os.getenv("SHOW_SHEET_RECENTPICS_TTL_SEC", "300") or "300")
    show_cache_crop_on_fill: bool = _get_env_bool("SHOW_CACHE_CROP_ON_FILL", True)

    profile_messages: dict[str, int] = field(default_factory=lambda: {
        "1": 1361917184254935093, "2": 1361917363993182368, "4": 1361917392208531518,
        "5": 1361917398168371280, "6": 1361917404208304309, "7": 1361917410331856976,
        "9": 1361917519883010269, "17": 1361917533564702791, "67": 1361917567291363348,
    })

    # NLP
    nlp_model_path: str | None = os.getenv("NLP_MODEL_PATH") or os.getenv("DEBERTA_ONNX_PATH")
    nlp_tokenizer_path: str | None = os.getenv("NLP_TOKENIZER_PATH") or os.getenv("DEBERTA_TOKENIZER_JSON")
    nlp_conf_high: float = float(os.getenv("NLP_CONF_HIGH", "0.88"))
    nlp_conf_mid: float = float(os.getenv("NLP_CONF_MID", "0.75"))

    # Dues/Spam/Mail
    feed_lookback_minutes_before: int = int(os.getenv("FEED_LOOKBACK_MINUTES_BEFORE", "5"))
    feed_pending_minutes_after: int = int(os.getenv("FEED_PENDING_MINUTES_AFTER", "5"))
    trusted_role_names: list[str] = field(default_factory=lambda: ["due paying members", "server booster", "officers", "active feeders"])
    spam_ban_role_names: list[str] = field(default_factory=lambda: ["officers"])
    spam_min_account_days: int = int(os.getenv("SPAM_MIN_ACCOUNT_DAYS", "30"))
    spam_nlp_conf: float = float(os.getenv("SPAM_NLP_CONF", "0.9"))
    spam_alert_user_id: int | None = int(os.getenv("SPAM_ALERT_USER_ID", "0")) or None
    gmail_enabled: bool = _get_env_bool("GMAIL_ENABLED", False)
    gmail_log_manual_delay_sec: float = float(os.getenv("GMAIL_LOG_MANUAL_DELAY_SEC", "0.25"))
    gmail_log_scheduler_delay_sec: float = float(os.getenv("GMAIL_LOG_SCHEDULER_DELAY_SEC", "10.0"))
    dues_enabled: bool = _get_env_bool("DUES_ENABLED", True)
    dues_email_window_days: int = int(os.getenv("DUES_EMAIL_WINDOW_DAYS", "3"))
    dues_scan_skip_oldest: int = int(os.getenv("DUES_SCAN_SKIP_OLDEST", "3"))
    dues_scan_limit: int = int(os.getenv("DUES_SCAN_LIMIT", "0"))
    dues_fast_map: bool = _get_env_bool("DUES_FAST_MAP", True)
    dues_membership_ttl_sec: int = int(os.getenv("DUES_MEMBERSHIP_TTL_SEC", "300") or "300")
    dues_allowed_amounts: list[int] = field(default_factory=lambda: [int(x) for x in _get_env_list("DUES_ALLOWED_AMOUNTS") if x.strip().lstrip("-").isdigit()] or [15, 20, 25])
    membership_ws_title: str = os.getenv("MEMBERSHIP_WS_TITLE", "Membership Application List")
    dues_nlp_enabled: bool = _get_env_bool("DUES_NLP_ENABLED", False)
    dues_nlp_max_calls: int = int(os.getenv("DUES_NLP_MAX_CALLS", "50"))
    cat_aliases_ttl_sec: int = int(os.getenv("CAT_ALIASES_TTL_SEC", "7200") or "7200")
    cat_profile_ttl_sec: int = int(os.getenv("CAT_PROFILE_TTL_SEC", "3600") or "3600")
    dues_member_max_candidates: int = int(os.getenv("DUES_MEMBER_MAX_CANDIDATES", "300"))

    # --- PERMISSIONS ---
    role_officer_id: int = 845035667661783061
    
    access_feeding_manager: list[int] = field(default_factory=lambda: [
        499034835562790912, # Megan
    ])
    
    access_photo_labeler: list[int] = field(default_factory=lambda: [
        528421517592363008, # Atlas
        624440365595754496, # Austin
        474329968936091648, # Miranda
    ])

    # Maps Names -> IDs for pinging
    user_id_map: Dict[str, int] = field(default_factory=lambda: {
    })

settings = Settings()

if not settings.tomcat_wake: settings.tomcat_wake = settings.bot_name
if not settings.sheet_catabase_id and settings.cat_spreadsheet_id: settings.sheet_catabase_id = settings.cat_spreadsheet_id
if not settings.sheet_vision_id and settings.aux_spreadsheet_id: settings.sheet_vision_id = settings.aux_spreadsheet_id