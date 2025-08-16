"""Centralized configuration loader.

Fill placeholders in .env. We avoid hard-coding IDs/tokens in source.
"""
from __future__ import annotations
import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()

@dataclass
class Settings:
    # Discord
    discord_token: str = os.getenv("DISCORD_TOKEN", "")
    command_prefix: str = os.getenv("COMMAND_PREFIX", "!")
    bot_name: str = os.getenv("BOT_NAME", "tomcat")
    timezone: str = os.getenv("TIMEZONE", "America/Chicago")

    # Channels
    ch_due_portal: int | None = int(os.getenv("CH_DUE_PORTAL", "0")) or None
    ch_feeding_team: int | None = int(os.getenv("CH_FEEDING_TEAM", "0")) or None
    ch_pictures_of_cats: int | None = int(os.getenv("CH_PICTURES_OF_CATS", "0")) or None
    ch_report_new_cats: int | None = int(os.getenv("CH_REPORT_NEW_CATS", "0")) or None
    ch_member_names: int | None = int(os.getenv("CH_MEMBER_NAMES", "0")) or None
    ch_logging: int | None = int(os.getenv("CH_LOGGING", "0")) or None
    ch_sandbox: int | None = int(os.getenv("CH_TOMCAT_SANDBOX", "0")) or None

    # Google Sheets
    google_sa_json: str = "credentials/service_account.json"
    cat_spreadsheet_id: str | None = os.getenv("CAT_SPREADSHEET_ID")
    aux_spreadsheet_id: str | None = os.getenv("AUX_SPREADSHEET_ID")

    # Logging
    log_dir: str = os.getenv("LOG_DIR", "./logs")

    # Channels where misc handlers like "meow" are allowed.
    # Add channel IDs here, e.g. {1234567890, 9876543210}
    misc_channels: set[int] = field(default_factory=set)

settings = Settings()