#tomcat/config.py
"""
Configuration and tuning knobs for TomCat.

Future idea: Intent policy map
--------------------------------
If you later want to tune behavior without code changes, consider adding an
`intent_policy` structure here which the router can read, e.g.:

    intent_policy = {
        "cv_identify": {"require_wake": True},
        "show_photo":  {"require_wake": True},
        "who_is":      {"require_wake": True},
        "feeding_status": {"require_wake": True},
        "feed_update": {"allowed_channels": [CH_FEEDING_TEAM]},
        "sub_request": {"allowed_channels": [CH_FEEDING_TEAM]},
        "sub_accept":  {"allowed_channels": [CH_FEEDING_TEAM]},
    }

By expressing wake requirements and allowed channels here, you can flip
policies via environment variables without code edits. For now, the router
implements the equivalent logic inline.
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
    """Parse a list env like "[CH_FEEDING_TEAM, CH_TOMCAT_SANDBOX]" or "123,456" into ints.
    Each token may be an env var name whose value is a numeric ID, or a numeric string.
    """
    raw = (os.getenv(key, "") or "").strip()
    if not raw:
        return []
    #strip brackets if present
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    toks = [t.strip() for t in raw.split(",") if t.strip()]
    out: list[int] = []
    for t in toks:
        val = os.getenv(t, t)  #if token is an env var name, use its value; else the token itself
        try:
            cid = int(str(val).strip())
            if cid:
                out.append(cid)
        except Exception:
            continue
    return out

def _build_channel_sheet_map() -> dict[int, str]:
    """
    Build channel->sheetTab map from env.
    Supports either:
      - CHANNEL_SHEET_MAP="CH_PICTURES_OF_CATS:TCBPicsInput,CH_REPORT_NEW_CATS:TCBPicsInput,1344745306620694558:TCBVetBillInput"
        (left side can be an env var name or a raw numeric ID)
      - Or, if unset, a sane default using named channels.
    """
    raw = os.getenv("CHANNEL_SHEET_MAP", "").strip()
    out: dict[int, str] = {}
    if raw:
        out: Dict[int, str] = {}
        for pair in (p.strip() for p in raw.split(",") if p.strip()):
            if ":" not in pair:
                continue
            k, tab = (s.strip() for s in pair.split(":", 1))
            chan = os.getenv(k) if not k.isdigit() else k
            if not chan:
                continue
            try:
                cid = int(chan)
            except Exception:
                continue
            if cid and tab:
                out[cid] = tab
        if out:
            return out
    #fallback defaults from the configured channel names
    def _id(name: str) -> int | None:
        try:
            v = int(os.getenv(name, "0"))
            return v or None
        except Exception:
            return None
    pics = _id("CH_PICTURES_OF_CATS")
    rpt  = _id("CH_REPORT_NEW_CATS")
    if pics: out[pics] = "TCBPicsInput"
    if rpt:  out[rpt]  = "TCBPicsInput"
    #add more named channels later if you introduce them (e.g., CH_VET_BILLS -> "TCBVetBillInput")
    return out


@dataclass
class Settings:
    #Discord
    discord_token: str = os.getenv("DISCORD_TOKEN", "")
    command_prefix: str = os.getenv("COMMAND_PREFIX", "!")
    bot_name: str = os.getenv("BOT_NAME", "tomcat")
    tomcat_wake: str = os.getenv("TOMCAT_WAKE", os.getenv("BOT_NAME", "tomcat"))
    #Bot IDs (fallbacks provided per user notes; override via env in prod)
    bot_user_id: int | None = int(os.getenv("BOT_USER_ID", "1341667150066225192") or "0") or None
    bot_dm_id: int | None = int(os.getenv("BOT_DM_ID", "1352882061651873863") or "0") or None
    timezone: str = os.getenv("TIMEZONE", "America/Chicago")
    channel_sheet_map: dict[int, str] = field(default_factory=_build_channel_sheet_map)
    dm_image_tab: str = os.getenv("DM_IMAGE_TAB", "TCBPicsInput")
    dm_image_sheet_id: str | None = os.getenv("DM_IMAGE_SHEET_ID") or None
    #Admins
    admin_ids: list[int] = field(default_factory=lambda: [
        int(x) for x in _get_env_list("ADMIN_IDS") if x.strip().lstrip("-").isdigit()
    ])
    silent_mode: bool = field(default_factory=lambda: _get_env_bool("SILENT_MODE", False))

    #Channels
    ch_due_portal: int | None = int(os.getenv("CH_DUE_PORTAL", "0")) or None
    ch_feeding_team: int | None = int(os.getenv("CH_FEEDING_TEAM", "0")) or None
    ch_pictures_of_cats: int | None = int(os.getenv("CH_PICTURES_OF_CATS", "0")) or None
    ch_report_new_cats: int | None = int(os.getenv("CH_REPORT_NEW_CATS", "0")) or None
    ch_member_names: int | None = int(os.getenv("CH_MEMBER_NAMES", "0")) or None
    ch_logging: int | None = int(os.getenv("CH_LOGGING", "0")) or None
    ch_sandbox: int | None = int(os.getenv("CH_TOMCAT_SANDBOX", "0")) or None
    #Primary guild (server) used for admin tasks
    target_guild_id: int | None = int(os.getenv("TARGET_GUILD_ID", "0") or "0") or None
    #UI/Auth: guild + roles used by the web UI permissions
    ui_guild_id: int | None = int(os.getenv("UI_GUILD_ID", "0") or "0") or None
    officer_role_id: int | None = int(os.getenv("OFFICER_ROLE_ID", "0") or "0") or None
    role_feeding_manager_id: int | None = int(os.getenv("ROLE_FEEDING_MANAGER", "0") or "0") or None
    role_photo_labeler_id: int | None = int(os.getenv("ROLE_PHOTO_LABELER", "0") or "0") or None
    role_viewer_id: int | None = int(os.getenv("ROLE_VIEWER", "0") or "0") or None
    role_due_paying_id: int | None = int(os.getenv("ROLE_DUE_PAYING", "0") or "0") or None
    role_holiday_feeder_id: int | None = int(os.getenv("ROLE_HOLIDAY_FEEDER", "0") or "0") or None
    role_dues_perks_id: int | None = int(os.getenv("ROLE_DUES_PERKS", "0") or "0") or None
    #Channels allowed to mark feed updates (default empty → no restriction). You set this in .env as
    #allowed_feeding_channel_ids=[CH_FEEDING_TEAM, CH_TOMCAT_SANDBOX]
    allowed_feeding_channel_ids: list[int] = field(default_factory=lambda: _parse_channel_list_env("allowed_feeding_channel_ids"))

    #Google service account
    google_service_account_json: str = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "credentials/service_account.json")
    #Retain the legacy name as well
    google_sa_json: str = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "credentials/service_account.json")

    #Sheets (support both old and new env names)
    #Legacy names in .env: SHEET_CATABASE_ID, SHEET_VISION_ID, SHEET_MEGASHEET_ID
    sheet_catabase_id: str | None = os.getenv("SHEET_CATABASE_ID") or os.getenv("CAT_SPREADSHEET_ID")
    sheet_vision_id: str | None = os.getenv("SHEET_VISION_ID") or os.getenv("AUX_SPREADSHEET_ID")
    sheet_megasheet_id: str | None = os.getenv("SHEET_MEGASHEET_ID")

    #Also keep the new-style names some modules were using
    cat_spreadsheet_id: str | None = os.getenv("CAT_SPREADSHEET_ID") or os.getenv("SHEET_CATABASE_ID")
    aux_spreadsheet_id: str | None = os.getenv("AUX_SPREADSHEET_ID") or os.getenv("SHEET_VISION_ID")

    finance_sheet_throttle_sec: float = float(os.getenv("FINANCE_SHEET_THROTTLE_SEC", "0.5") or "0.5")

    #Logging
    log_dir: str = os.getenv("LOG_DIR", "./logs")

    #Channels where misc handlers like "meow" are allowed (empty set means everywhere)
    misc_channels: set[int] = field(default_factory=set)

        #======== CV CONFIG (v5.6 parity; easy to tweak) ========
    #Paths (drop-and-play). Change these when you retrain/move weights.
    cv_detect_weights: str = os.getenv(
        "CV_DETECT_WEIGHTS",
        os.path.join("weights", "NanoModel.pt"),
    )
    cv_classify_weights: str = os.getenv(
        "CV_CLASSIFY_WEIGHTS",
        os.path.join("weights", "NanoClassifier.pt"),
    )

    #Optional human-readable class names for the classifier. Leave empty to use Cat{idx}.
    cv_class_names: list[str] = field(default_factory=lambda: [
        "Microwave", "Faye", "Bobbie", "Twix", "Citlali", "Angel", "Winston", "Radar", "Eggs", "Dumpster",
        "Gregory", "Rubber", "Bruno", "Boots", "Princess", "Nefarious", "Eraser", "Eden", "Cassie", "Coronavirus"
    ])

    #Core knobs (defaults mirror v5.6 values)
    cv_conf: float = float(os.getenv("CV_CONF", "0.552"))           #detector confidence
    cv_iou: float = float(os.getenv("CV_IOU", "0.45"))              #NMS IoU
    cv_detect_imgsz: int = int(os.getenv("CV_DETECT_IMGSZ", "640")) #YOLO inference size
    cv_clf_imgsz: int = int(os.getenv("CV_CLF_IMGSZ", "640"))       #classifier input size
    cv_pad_pct: float = float(os.getenv("CV_PAD_PCT", "0.03"))      #crop expansion

    #Safety/limits
    cv_max_image_dim: int = int(os.getenv("CV_MAX_IMAGE_DIM", "10000"))   #10K cap on longest side
    cv_max_download_mb: int = int(os.getenv("CV_MAX_DOWNLOAD_MB", "16")) #attachment size cap

    #Device/precision
    cv_half: bool = os.getenv("CV_FP16", "1").strip().lower() in {"1","true","yes","on"}

    #Temp folder for downloads (repo-local, not hidden OS temp)
    cv_temp_dir: str = os.getenv("CV_TEMP_DIR", "./temp_images")
    #Verbose crop logging (disable during bulk cache fills)
    cv_log_crop: bool = _get_env_bool("CV_LOG_CROP", True)
    #Auto-crop for "show me" / "who is"
    auto_crop_show_photo: bool = os.getenv("AUTO_CROP_SHOW_PHOTO", "1").strip().lower() in {"1","true","yes","on"}
    #Hard budget for auto-crop work in handlers (ms). If exceeded, show original image.
    cv_timeout_ms: int = int(os.getenv("CV_TIMEOUT_MS", "6000"))

    #Show-photo cache (for fast "show me" responses)
    show_cache_dir: str = os.getenv("SHOW_CACHE_DIR", "./cache/show_photos")
    show_cache_per_cat: int = int(os.getenv("SHOW_CACHE_PER_CAT", "5") or "5")
    show_cache_prefill_on_boot: bool = _get_env_bool("SHOW_CACHE_PREFILL_ON_BOOT", True)
    show_cache_warm_concurrency: int = int(os.getenv("SHOW_CACHE_WARM_CONCURRENCY", "2") or "2")
    show_cache_warm_limit: int = int(os.getenv("SHOW_CACHE_WARM_LIMIT", "0") or "0")  #0 = all
    #Optional: downscale/compress cached JPEGs to speed sending (0 disables)
    show_cache_resize_max_dim: int = int(os.getenv("SHOW_CACHE_RESIZE_MAX_DIM", "0") or "0")
    show_cache_jpeg_quality: int = int(os.getenv("SHOW_CACHE_JPEG_QUALITY", "88") or "88")
    #Use a small TTL to reuse RecentPics sheet rows in-process and avoid repeated network calls
    show_sheet_recentpics_ttl_sec: int = int(os.getenv("SHOW_SHEET_RECENTPICS_TTL_SEC", "300") or "300")
    #Optionally skip auto-crop during cache fill to speed up
    show_cache_crop_on_fill: bool = _get_env_bool("SHOW_CACHE_CROP_ON_FILL", True)

    #CV pairing windows (tunable without code)
    cv_lookback_seconds_before: int = int(os.getenv("CV_LOOKBACK_SECONDS_BEFORE", "30"))
    cv_pending_minutes_after: int = int(os.getenv("CV_PENDING_MINUTES_AFTER", "5"))

    #Stored profile message IDs from v5.6 (cat ID -> Discord message ID)
    profile_messages: dict[str, int] = field(default_factory=lambda: {
        "1": 1361917184254935093,
        "2": 1361917363993182368,
        "4": 1361917392208531518,
        "5": 1361917398168371280,
        "6": 1361917404208304309,
        "7": 1361917410331856976,
        "9": 1361917519883010269,
        "17": 1361917533564702791,
        "67": 1361917567291363348,
    })

    #======== NLP CONFIG (optional DeBERTa ONNX) ========
    #If provided, we enable zero-shot intent + entity scoring via ONNXRuntime.
    nlp_model_path: str | None = os.getenv("NLP_MODEL_PATH") or os.getenv("DEBERTA_ONNX_PATH")
    nlp_tokenizer_path: str | None = os.getenv("NLP_TOKENIZER_PATH") or os.getenv("DEBERTA_TOKENIZER_JSON")
    nlp_conf_high: float = float(os.getenv("NLP_CONF_HIGH", "0.88"))
    nlp_conf_mid: float = float(os.getenv("NLP_CONF_MID", "0.75"))

    #======== Feeding windows ========
    feed_lookback_minutes_before: int = int(os.getenv("FEED_LOOKBACK_MINUTES_BEFORE", "5"))
    feed_pending_minutes_after: int = int(os.getenv("FEED_PENDING_MINUTES_AFTER", "5"))

    #======== Spam detection config ========
    #Role names that should never be flagged as spam (case-insensitive contains match)
    trusted_role_names: list[str] = field(default_factory=lambda: [
        "due paying members", "server booster", "officers", "active feeders"
    ])
    spam_ban_role_names: list[str] = field(default_factory=lambda: [
        "officers"
    ])
    #Minimum account age in days to skip spam checks
    spam_min_account_days: int = int(os.getenv("SPAM_MIN_ACCOUNT_DAYS", "30"))
    #NLP spam threshold if ONNX model is configured
    spam_nlp_conf: float = float(os.getenv("SPAM_NLP_CONF", "0.9"))
    #Optional: user to ping on spam alerts (falls back to first admin)
    spam_alert_user_id: int | None = int(os.getenv("SPAM_ALERT_USER_ID", "0")) or None

    #======== Gmail / Email logging ========
    gmail_enabled: bool = _get_env_bool("GMAIL_ENABLED", False)
    gmail_log_manual_delay_sec: float = float(os.getenv("GMAIL_LOG_MANUAL_DELAY_SEC", "0.25"))
    gmail_log_scheduler_delay_sec: float = float(os.getenv("GMAIL_LOG_SCHEDULER_DELAY_SEC", "10.0"))

    #======== Dues matching / portal ========
    dues_enabled: bool = _get_env_bool("DUES_ENABLED", True)
    dues_email_window_days: int = int(os.getenv("DUES_EMAIL_WINDOW_DAYS", "3"))
    dues_scan_skip_oldest: int = int(os.getenv("DUES_SCAN_SKIP_OLDEST", "3"))
    dues_scan_limit: int = int(os.getenv("DUES_SCAN_LIMIT", "0"))  #0 = no limit (all)
    #Use a lighter, heuristic mapping in dues_check to avoid heavy fuzzy across all members
    dues_fast_map: bool = _get_env_bool("DUES_FAST_MAP", True)
    #Cache membership sheet reads briefly to avoid 429s on repeated runs
    dues_membership_ttl_sec: int = int(os.getenv("DUES_MEMBERSHIP_TTL_SEC", "300") or "300")
    dues_allowed_amounts: list[int] = field(default_factory=lambda: [
        int(x) for x in _get_env_list("DUES_ALLOWED_AMOUNTS") if x.strip().lstrip("-").isdigit()
    ] or [15, 20, 25])
    membership_ws_title: str = os.getenv("MEMBERSHIP_WS_TITLE", "Membership Application List")
    dues_nlp_enabled: bool = _get_env_bool("DUES_NLP_ENABLED", False)
    dues_nlp_max_calls: int = int(os.getenv("DUES_NLP_MAX_CALLS", "50"))
    #Cat aliases/profile TTLs
    cat_aliases_ttl_sec: int = int(os.getenv("CAT_ALIASES_TTL_SEC", "7200") or "7200")
    cat_profile_ttl_sec: int = int(os.getenv("CAT_PROFILE_TTL_SEC", "3600") or "3600")
    dues_member_max_candidates: int = int(os.getenv("DUES_MEMBER_MAX_CANDIDATES", "300"))

    #Roles & Guilds
    role_officer: int = int(os.getenv("ROLE_OFFICER", "0"))
    role_dues: int = int(os.getenv("ROLE_DUES", "0"))
    role_feeding_manager: int = int(os.getenv("ROLE_FEEDING_MANAGER", "0"))
    role_photo_labeler: int = int(os.getenv("ROLE_PHOTO_LABELER", "0"))
    role_viewer: int = int(os.getenv("ROLE_VIEWER", "0"))
    role_holiday: int = int(os.getenv("ROLE_HOLIDAY", "0"))
    main_guild_id: int = int(os.getenv("MAIN_GUILD_ID", "0"))



    #======== Feeding scheduler maps (authoritative) ========
    #Provide simple name→user_id mapping and per-station weekly assignments.
    #Station assignments are lists of 7 names ordered Sun..Sat.

    user_id_map: Dict[str, int] = field(default_factory=dict)

    feeding_schedule: Dict[str, list[str]] = field(default_factory=dict)





settings = Settings()



#Back-compat touches
if not settings.tomcat_wake:
    settings.tomcat_wake = settings.bot_name
#Ensure both sheet_* and *_spreadsheet_id are aligned
if not settings.sheet_catabase_id and settings.cat_spreadsheet_id:
    settings.sheet_catabase_id = settings.cat_spreadsheet_id
if not settings.sheet_vision_id and settings.aux_spreadsheet_id:
    settings.sheet_vision_id = settings.aux_spreadsheet_id
#UI defaults: fall back to target guild if a UI-specific guild ID is not provided
if not settings.ui_guild_id and settings.target_guild_id:
    settings.ui_guild_id = settings.target_guild_id
