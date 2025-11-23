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

    # --- SHEET IDS ---
    sheet_catabase_id: str | None = os.getenv("SHEET_CATABASE_ID") or os.getenv("CAT_SPREADSHEET_ID")
    sheet_vision_id: str | None = os.getenv("SHEET_VISION_ID") or os.getenv("AUX_SPREADSHEET_ID")
    
    channel_sheet_map: dict[int, str] = field(default_factory=_build_channel_sheet_map)

    # --- CHANNELS ---
    ch_feeding_team: int | None = int(os.getenv("CH_FEEDING_TEAM", "0") or "0") or None
    ch_sandbox: int | None = int(os.getenv("CH_TOMCAT_SANDBOX", "0") or "0") or None
    ch_logging: int | None = int(os.getenv("CH_LOGGING", "0") or "0") or None
    
    # --- ROLES & PERMISSIONS (HARDCODED AS REQUESTED) ---
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
    
    # CV / ML Settings
    cv_detect_weights: str = os.path.join("weights", "NanoModel.pt")
    cv_classify_weights: str = os.path.join("weights", "NanoClassifier.pt")
    cv_conf: float = 0.552
    cv_class_names: list[str] = field(default_factory=lambda: [
        "Microwave", "Faye", "Bobbie", "Twix", "Citlali", "Angel", "Winston", "Radar", "Eggs", "Dumpster",
        "Gregory", "Rubber", "Bruno", "Boots", "Princess", "Nefarious", "Eraser", "Eden", "Cassie", "Coronavirus"
    ])
    
    # Spam / Mail
    gmail_enabled: bool = False
    dues_enabled: bool = True
    
    misc_channels: set[int] = field(default_factory=set)

settings = Settings()