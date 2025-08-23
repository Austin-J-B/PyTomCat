"""Centralized configuration loader."""
from __future__ import annotations
import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()

@dataclass
class Settings:
    discord_token: str = os.getenv("DISCORD_TOKEN", "")
    command_prefix: str = os.getenv("COMMAND_PREFIX", "!")
    bot_name: str = os.getenv("BOT_NAME", "tomcat")
    timezone: str = os.getenv("TIMEZONE", "America/Chicago")

    ch_feeding_team: int | None = int(os.getenv("CH_FEEDING_TEAM", "0")) or None
    ch_pictures_of_cats: int | None = int(os.getenv("CH_PICTURES_OF_CATS", "0")) or None
    ch_report_new_cats: int | None = int(os.getenv("CH_REPORT_NEW_CATS", "0")) or None
    ch_member_names: int | None = int(os.getenv("CH_MEMBER_NAMES", "0")) or None
    ch_logging: int | None = int(os.getenv("CH_LOGGING", "0")) or None
    ch_sandbox: int | None = int(os.getenv("CH_TOMCAT_SANDBOX", "0")) or None
    ch_dues_portal: int | None = int(os.getenv("CH_DUES_PORTAL", "0")) or None

    gmail_enabled: bool = os.getenv("GMAIL_ENABLED", "0") == "1"
    gmail_credentials_path: str = os.getenv("GMAIL_CREDENTIALS_PATH", "./.gmail_credentials.json")
    gmail_token_path: str = os.getenv("GMAIL_TOKEN_PATH", "./.gmail_token.json")
    gmail_query: str = os.getenv(
        "GMAIL_QUERY",
        'newer_than:7d ("from:paypal.com" OR "from:venmo.com" OR "from:cash.app")',
    )
    dues_currency: str = os.getenv("DUES_CURRENCY", "USD")

    admin_ids: set[int] = field(default_factory=lambda: {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()})

    silent_mode: bool = False

    google_sa_json: str = "credentials/service_account.json"
    cat_spreadsheet_id: str | None = os.getenv("CAT_SPREADSHEET_ID")
    aux_spreadsheet_id: str | None = os.getenv("AUX_SPREADSHEET_ID")

    log_dir: str = os.getenv("LOG_DIR", "./logs")
    misc_channels: set[int] = field(default_factory=set)

settings = Settings()
