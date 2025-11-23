from __future__ import annotations
import os
from dataclasses import dataclass, field
from dotenv import load_dotenv
from typing import Dict

load_dotenv()

def _get_env_bool(key: str, default: bool = False) -> bool:
    v = os.getenv(key)
    if v is None: return default
    return v.strip().lower() in {"1", "true", "yes", "on"}

def _get_env_list(key: str, sep: str = ",") -> list[str]:
    raw = os.getenv(key, "")
    return [s.strip() for s in raw.split(sep) if s.strip()]

def _build_channel_sheet_map() -> dict[int, str]:
    raw = os.getenv("CHANNEL_SHEET_MAP", "").strip()
    out: dict[int, str] = {}
    if raw:
        for pair in (p.strip() for p in raw.split(",") if p.strip()):
            if ":" not in pair: continue
            k, tab = (s.strip() for s in pair.split(":", 1))
            try: out[int(os.getenv(k) or k)] = tab
            except: continue
    return out

@dataclass
class Settings:
    # --- DISCORD & AUTH ---
    discord_token: str = os.getenv("DISCORD_TOKEN", "")
    discord_client_secret: str = os.getenv("DISCORD_CLIENT_SECRET", "")
    ui_activity_app_id: str = os.getenv("UITEST_ACTIVITY_APP_ID", "")
    bot_user_id: int = int(os.getenv("BOT_USER_ID", "0") or "0")
    command_prefix: str = os.getenv("COMMAND_PREFIX", "!")
    timezone: str = os.getenv("TIMEZONE", "America/Chicago")
    
    # --- PATHS & FILES ---
    schedule_file: str = os.getenv("SCHEDULE_FILE", "data/feeding_schedule.json")
    log_dir: str = os.getenv("LOG_DIR", "./logs")
    google_service_account_json: str = "credentials/service_account.json"
    # Backwards compatibility for sheets_client.py
    google_sa_json: str = "credentials/service_account.json" 

    # --- SHEET IDS ---
    sheet_catabase_id: str | None = os.getenv("SHEET_CATABASE_ID") or os.getenv("CAT_SPREADSHEET_ID")
    sheet_vision_id: str | None = os.getenv("SHEET_VISION_ID") or os.getenv("AUX_SPREADSHEET_ID")
    sheet_megasheet_id: str | None = os.getenv("SHEET_MEGASHEET_ID")
    
    # Compat aliases
    cat_spreadsheet_id: str | None = sheet_catabase_id
    aux_spreadsheet_id: str | None = sheet_vision_id
    
    channel_sheet_map: dict[int, str] = field(default_factory=_build_channel_sheet_map)
    finance_sheet_throttle_sec: float = 0.5

    # --- CHANNELS ---
    ch_feeding_team: int | None = int(os.getenv("CH_FEEDING_TEAM", "0") or "0") or None
    ch_sandbox: int | None = int(os.getenv("CH_TOMCAT_SANDBOX", "0") or "0") or None
    ch_logging: int | None = int(os.getenv("CH_LOGGING", "0") or "0") or None
    # Used by misc handler
    misc_channels: set[int] = field(default_factory=set) 
    
    # --- ROLES & PERMISSIONS ---
    role_officer_id: int = 845035667661783061
    
    access_feeding_manager: list[int] = field(default_factory=lambda: [
        499034835562790912, # Megan
    ])
    
    access_photo_labeler: list[int] = field(default_factory=lambda: [
        528421517592363008, # Atlas
        624440365595754496, # Austin
        474329968936091648, # Miranda
    ])

    # --- PING MAP (Name -> Discord ID) ---
    user_id_map: Dict[str, int] = field(default_factory=lambda: {
        "Nicole": 1308894473228648536, "Lynn": 699720057764446221, "Atlas": 528421517592363008,
        "CiCi": 342386549532524544, "Roach": 674640043289083944, "Elusive": 751926923583553656,
        "Miranda": 474329968936091648, "Ben": 972653971728633896, "Brooke": 1014214516764053614,
        "Alex": 564615306027335681, "Morgan": 856586084943396879, "Anabelle": 808757369478840371,
        "Zahara": 1004778582855389244, "Bryan": 204682859217158144, "Jaeden": 417059337257877505,
        "Kitadan": 427867525225906176, "Felix": 694664394495361195, "Izzy": 891876061313380425,
        "Kaz": 356861356051529750, "Thorin": 980567857849045032, "Acacia": 543969877619245068,
        "Rinne": 63085459886055424, "Emmaleigh": 338109126808829953, "Alexa": 518999622916505633,
        "Bunny": 609945813128445974, "Abigail": 568451921828904962, "Zoe": 749751349679358064,
        "Isabella": 963624067078971402, "Julia": 760947102280319008, "Micaela": 741877766030491678,
        "Peter": 750387156920303798, "Victoria": 732664872172519464, "Brian": 642133560685494282,
        "Sophia": 690727460924424212, "Charlotte": 748739000914935828, "Autumn": 1410304461707940022,
        "Michael": 426919280378904588, "Loren": 413721884107210753, "Lucas": 802391113050226708,
        "Emma": 722682931704889345, "Jack": 1037772447509917717, "Megan": 788886705276846140,
    })

    # --- OTHER SETTINGS ---
    bot_name: str = "tomcat"
    tomcat_wake: str = "tomcat"
    silent_mode: bool = False
    
    # --- CACHE & CV (Restored to fix AttributeError) ---
    show_cache_dir: str = "./cache/show_photos"
    show_cache_per_cat: int = 5
    show_cache_prefill_on_boot: bool = True  # This was the missing one
    show_cache_warm_concurrency: int = 2
    show_cache_warm_limit: int = 0
    show_cache_resize_max_dim: int = 0
    show_cache_jpeg_quality: int = 88
    show_sheet_recentpics_ttl_sec: int = 300
    show_cache_crop_on_fill: bool = True
    
    cv_detect_weights: str = os.path.join("weights", "NanoModel.pt")
    cv_classify_weights: str = os.path.join("weights", "NanoClassifier.pt")
    cv_conf: float = 0.552
    cv_iou: float = 0.45
    cv_detect_imgsz: int = 640
    cv_clf_imgsz: int = 640
    cv_pad_pct: float = 0.03
    cv_max_image_dim: int = 10000
    cv_max_download_mb: int = 16
    cv_half: bool = True
    cv_temp_dir: str = "./temp_images"
    cv_log_crop: bool = True
    auto_crop_show_photo: bool = True
    cv_timeout_ms: int = 6000
    cv_lookback_seconds_before: int = 30
    cv_pending_minutes_after: int = 5
    
    cv_class_names: list[str] = field(default_factory=lambda: [
        "Microwave", "Faye", "Bobbie", "Twix", "Citlali", "Angel", "Winston", "Radar", "Eggs", "Dumpster",
        "Gregory", "Rubber", "Bruno", "Boots", "Princess", "Nefarious", "Eraser", "Eden", "Cassie", "Coronavirus"
    ])
    
    profile_messages: dict[str, int] = field(default_factory=lambda: {
        "1": 1361917184254935093, "2": 1361917363993182368, "4": 1361917392208531518,
        "5": 1361917398168371280, "6": 1361917404208304309, "7": 1361917410331856976,
        "9": 1361917519883010269, "17": 1361917533564702791, "67": 1361917567291363348,
    })

    # --- SPAM / MAIL / DUES ---
    gmail_enabled: bool = False
    dues_enabled: bool = True
    
    trusted_role_names: list[str] = field(default_factory=lambda: ["due paying members", "server booster", "officers", "active feeders"])
    spam_ban_role_names: list[str] = field(default_factory=lambda: ["officers"])
    spam_min_account_days: int = 30
    spam_nlp_conf: float = 0.9
    spam_alert_user_id: int | None = None
    
    dues_email_window_days: int = 3
    dues_scan_skip_oldest: int = 3
    dues_scan_limit: int = 0
    dues_fast_map: bool = True
    dues_membership_ttl_sec: int = 300
    dues_allowed_amounts: list[int] = field(default_factory=lambda: [15, 20, 25])
    membership_ws_title: str = "Membership Application List"
    dues_nlp_enabled: bool = False
    dues_nlp_max_calls: int = 50
    cat_aliases_ttl_sec: int = 7200
    cat_profile_ttl_sec: int = 3600
    dues_member_max_candidates: int = 300
    
    # NLP
    nlp_model_path: str | None = os.getenv("NLP_MODEL_PATH")
    nlp_tokenizer_path: str | None = os.getenv("NLP_TOKENIZER_PATH")
    nlp_conf_high: float = 0.88
    nlp_conf_mid: float = 0.75

settings = Settings()