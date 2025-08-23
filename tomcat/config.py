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

settings = Settings()

# Back-compat touches
if not settings.tomcat_wake:
    settings.tomcat_wake = settings.bot_name
# Ensure both sheet_* and *_spreadsheet_id are aligned
if not settings.sheet_catabase_id and settings.cat_spreadsheet_id:
    settings.sheet_catabase_id = settings.cat_spreadsheet_id
if not settings.sheet_vision_id and settings.aux_spreadsheet_id:
    settings.sheet_vision_id = settings.aux_spreadsheet_id
