"""Centralized configuration loader.

Fill placeholders in .env. We avoid hard-coding IDs/tokens in source.
"""
from __future__ import annotations
import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()

def _get_env_list(name: str) -> list[str]:
    """Gets a comma-separated list from an environment variable."""
    val = os.getenv(name)
    if not val:
        return []
    return [item.strip() for item in val.split(',')]

def _get_env_bool(name: str, default: bool = False) -> bool:
    """Gets a boolean from an environment variable."""
    val = os.getenv(name)
    if val is None:
        return default
    return val.lower() in ('true', '1', 't', 'yes')

@dataclass
class Settings:
    # General
    discord_token: str = os.getenv("DISCORD_TOKEN", "")
    command_prefix: str = os.getenv("COMMAND_PREFIX", "!")
    tomcat_wake: str = os.getenv("TOMCAT_WAKE", "TomCat")
    timezone: str = os.getenv("TIMEZONE", "America/Chicago")
    admin_ids: list[str] = field(default_factory=lambda: _get_env_list("ADMIN_IDS"))
    silent_mode: bool = field(default_factory=lambda: _get_env_bool("SILENT_MODE"))

    # Channels
    ch_feeding_team: int = int(os.getenv("CH_FEEDING_TEAM", 0))
    ch_tomcat_sandbox: int = int(os.getenv("CH_TOMCAT_SANDBOX", 0))
    ch_pictures_of_cats: int = int(os.getenv("CH_PICTURES_OF_CATS", 0))
    ch_report_new_cats: int = int(os.getenv("CH_REPORT_NEW_CATS", 0))
    ch_due_portal: int = int(os.getenv("CH_DUE_PORTAL", 0))
    ch_logging: int = int(os.getenv("CH_LOGGING", 0))
    ch_member_names: int = int(os.getenv("CH_MEMBER_NAMES", 0))

    # Google
    google_service_account_json: str = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    sheet_catabase_id: str = os.getenv("SHEET_CATABASE_ID", "")
    sheet_vision_id: str = os.getenv("SHEET_VISION_ID", "")
    sheet_megasheet_id: str = os.getenv("SHEET_MEGASHEET_ID", "")

settings = Settings()
