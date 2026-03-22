"""Sync derived CatDatabase columns from local photo metadata."""

from __future__ import annotations

import asyncio
import csv
import datetime as dt
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

import requests

from ..config import settings
from ..logger import log_action
from ..utils.datetime_utils import format_mmddyyyy
from . import local_photos
from .sheets_client import sheets_client

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - Python without zoneinfo
    ZoneInfo = None  # type: ignore


_SYNC_LOCK = threading.Lock()
_CAT_PREFIX_RE = re.compile(r"^\s*\d+\s*[.)\-:]?\s*")
_LABEL_SPLIT_RE = re.compile(r"[|,;/]+")
_SKIP_LABEL_KEYS = {
    "",
    "rejected",
    "needsreview",
    "needs review",
    "notacat",
    "not a cat",
    "0notacat",
}
_CATDATABASE_SNAPSHOT_PATH = Path("cache/catabase/Catabase - CatDatabase.csv")
_WORKSHEET_TITLE = "CatDatabase"
_DISCORD_USER_CACHE: dict[str, str] = {}

_COL_FULL_NAME = 0
_COL_LAST_SEEN_DATE = 2
_COL_LAST_SEEN_TIME = 3
_COL_LAST_SEEN_BY = 4
_COL_NUM_PICS = 16


@dataclass(frozen=True)
class CatPhotoAggregate:
    count: int = 0
    latest_timestamp: dt.datetime | None = None
    latest_label_author: str = ""
    latest_author_id: str = ""
    latest_serial: int = 0


def _display_label(value: str) -> str:
    return _CAT_PREFIX_RE.sub("", str(value or "").strip(), count=1).strip()


def _normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", _display_label(value).lower())


def _parse_serial(value: str) -> int:
    digits = re.sub(r"\D", "", str(value or ""))
    if not digits:
        return 0
    try:
        return int(digits)
    except Exception:
        return 0


def _row_label_keys(raw_labels: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in _LABEL_SPLIT_RE.split(str(raw_labels or "")):
        key = _normalize_key(raw)
        if not key or key in _SKIP_LABEL_KEYS or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def _parse_timestamp(value: str) -> dt.datetime | None:
    return local_photos._parse_metadata_timestamp(value)


def _configured_timezone(tz_name: str | None = None) -> dt.tzinfo:
    tz_name = str(tz_name or getattr(settings, "timezone", "America/Chicago") or "").strip()
    if ZoneInfo and tz_name:
        try:
            return ZoneInfo(tz_name)
        except Exception:
            pass
    try:
        return dt.datetime.now().astimezone().tzinfo or dt.timezone.utc
    except Exception:  # pragma: no cover - extremely defensive fallback
        return dt.timezone.utc


def _format_local_time(value: dt.datetime) -> str:
    if os.name == "nt":
        return value.strftime("%#I:%M:%S %p")
    return value.strftime("%-I:%M:%S %p")


def _parse_sheet_datetime(date_text: str, time_text: str, tzinfo: dt.tzinfo) -> dt.datetime | None:
    date_text = str(date_text or "").strip()
    time_text = str(time_text or "").strip()
    if not date_text:
        return None
    for fmt in ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %I:%M %p", "%m/%d/%Y"):
        try:
            parsed = dt.datetime.strptime(f"{date_text} {time_text}".strip(), fmt)
            return parsed.replace(tzinfo=tzinfo)
        except Exception:
            continue
    return None


def build_photo_aggregates(metadata_rows: Iterable[Mapping[str, str]]) -> dict[str, CatPhotoAggregate]:
    """Aggregate photo counts + latest-seen metadata per cat label."""
    grouped: dict[str, CatPhotoAggregate] = {}
    for row in metadata_rows:
        row_dict = dict(row or {})
        label_keys = _row_label_keys(row_dict.get("Box Cat IDs", ""))
        if not label_keys:
            continue
        timestamp = _parse_timestamp(row_dict.get("Timestamp", ""))
        label_author = str(row_dict.get("Label Author", "") or "").strip()
        author_id = str(row_dict.get("Author ID", "") or "").strip()
        serial = _parse_serial(row_dict.get("Serial Number", ""))
        for key in label_keys:
            current = grouped.get(key, CatPhotoAggregate())
            latest_timestamp = current.latest_timestamp
            latest_label_author = current.latest_label_author
            latest_author_id = current.latest_author_id
            latest_serial = current.latest_serial
            is_newer = False
            if timestamp is not None:
                if latest_timestamp is None or timestamp > latest_timestamp:
                    is_newer = True
                elif latest_timestamp is not None and timestamp == latest_timestamp and serial > latest_serial:
                    is_newer = True
            if is_newer:
                latest_timestamp = timestamp
                latest_label_author = label_author
                latest_author_id = author_id
                latest_serial = serial
            grouped[key] = CatPhotoAggregate(
                count=current.count + 1,
                latest_timestamp=latest_timestamp,
                latest_label_author=latest_label_author,
                latest_author_id=latest_author_id,
                latest_serial=latest_serial,
            )
    return grouped


def _resolve_discord_usernames(user_ids: Iterable[str]) -> dict[str, str]:
    user_ids = [str(raw or "").strip() for raw in user_ids if str(raw or "").strip()]
    token = str(getattr(settings, "discord_token", "") or "").strip()
    if not token:
        return {}
    wanted = []
    for uid in user_ids:
        if not uid.isdigit() or uid in _DISCORD_USER_CACHE:
            continue
        wanted.append(uid)
    if wanted:
        session = requests.Session()
        session.headers.update({"Authorization": f"Bot {token}"})
        for uid in wanted:
            for _attempt in range(3):
                try:
                    resp = session.get(f"https://discord.com/api/v10/users/{uid}", timeout=10)
                except Exception:
                    break
                if resp.status_code == 429:
                    try:
                        retry_after = float((resp.json() or {}).get("retry_after") or 1.0)
                    except Exception:
                        retry_after = 1.0
                    time.sleep(max(0.25, retry_after))
                    continue
                if resp.status_code != 200:
                    break
                try:
                    data = resp.json() or {}
                except Exception:
                    data = {}
                display = str(data.get("username") or data.get("global_name") or "").strip()
                if display:
                    _DISCORD_USER_CACHE[uid] = display
                break
    return {uid: _DISCORD_USER_CACHE.get(uid, "") for uid in set(user_ids)}


def _ensure_len(row: list[str], size: int) -> list[str]:
    if len(row) >= size:
        return row
    return row + ([""] * (size - len(row)))


def build_catabase_sync_table(
    sheet_rows: list[list[str]],
    metadata_rows: Iterable[Mapping[str, str]],
    *,
    aggregates: dict[str, CatPhotoAggregate] | None = None,
    timezone_name: str | None = None,
    author_name_by_user_id: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Build updated CatDatabase rows plus Sheets payloads for C/D/E/Q."""
    if not sheet_rows:
        return {
            "rows": [],
            "cde_values": [],
            "q_values": [],
            "changed_rows": 0,
            "data_rows": 0,
            "matched_cats": 0,
        }

    aggregates = aggregates if aggregates is not None else build_photo_aggregates(metadata_rows)
    tzinfo = _configured_timezone(timezone_name)
    out_rows: list[list[str]] = [list(sheet_rows[0])]
    cde_values: list[list[Any]] = []
    q_values: list[list[Any]] = []
    changed_rows = 0
    matched_cats = 0

    for src_row in sheet_rows[1:]:
        row = _ensure_len(list(src_row), _COL_NUM_PICS + 1)
        key = _normalize_key(row[_COL_FULL_NAME] if len(row) > _COL_FULL_NAME else "")
        aggregate = aggregates.get(key)
        new_date = str(row[_COL_LAST_SEEN_DATE] or "")
        new_time = str(row[_COL_LAST_SEEN_TIME] or "")
        new_by = str(row[_COL_LAST_SEEN_BY] or "")
        new_count: Any = _parse_serial(str(row[_COL_NUM_PICS] or ""))
        if aggregate is not None:
            matched_cats += 1
            new_count = max(int(new_count), int(aggregate.count))
            if aggregate.latest_timestamp is not None:
                local_dt = aggregate.latest_timestamp.astimezone(tzinfo)
                existing_dt = _parse_sheet_datetime(new_date, new_time, tzinfo)
                if existing_dt is None or local_dt >= existing_dt:
                    new_date = format_mmddyyyy(local_dt)
                    new_time = _format_local_time(local_dt)
                    resolved_name = ""
                    if author_name_by_user_id is not None:
                        resolved_name = str(author_name_by_user_id.get(aggregate.latest_author_id, "") or "").strip()
                    if resolved_name:
                        new_by = resolved_name
                    elif aggregate.latest_label_author:
                        new_by = aggregate.latest_label_author
                    elif existing_dt is not None and local_dt == existing_dt:
                        new_by = str(row[_COL_LAST_SEEN_BY] or "")
                    else:
                        new_by = ""
        old_tuple = (
            str(row[_COL_LAST_SEEN_DATE] or ""),
            str(row[_COL_LAST_SEEN_TIME] or ""),
            str(row[_COL_LAST_SEEN_BY] or ""),
            str(row[_COL_NUM_PICS] or ""),
        )
        new_tuple = (
            new_date,
            new_time,
            new_by,
            str(new_count),
        )
        if old_tuple != new_tuple:
            changed_rows += 1
        row[_COL_LAST_SEEN_DATE] = new_date
        row[_COL_LAST_SEEN_TIME] = new_time
        row[_COL_LAST_SEEN_BY] = new_by
        row[_COL_NUM_PICS] = str(new_count)
        out_rows.append(row)
        cde_values.append([new_date, new_time, new_by])
        q_values.append([new_count])

    return {
        "rows": out_rows,
        "cde_values": cde_values,
        "q_values": q_values,
        "changed_rows": changed_rows,
        "data_rows": max(0, len(sheet_rows) - 1),
        "matched_cats": matched_cats,
    }


def _write_snapshot(rows: list[list[str]]) -> None:
    if not rows:
        return
    try:
        _CATDATABASE_SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _CATDATABASE_SNAPSHOT_PATH.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerows(rows)
    except Exception:
        pass


def sync_catabase_photo_columns() -> dict[str, Any]:
    """Update CatDatabase C/D/E/Q from the local photo metadata CSV."""
    if not bool(getattr(settings, "catabase_photo_sync_enabled", True)):
        return {"status": "disabled"}

    sheet_id = str(getattr(settings, "sheet_catabase_id", "") or getattr(settings, "cat_spreadsheet_id", "") or "").strip()
    if not sheet_id:
        return {"status": "disabled_no_sheet"}

    metadata_path = local_photos.metadata_csv_path()
    if not metadata_path.is_file():
        return {"status": "missing_metadata_csv", "path": str(metadata_path)}

    metadata_rows = local_photos.read_metadata_rows()
    with _SYNC_LOCK:
        spreadsheet = sheets_client().open_by_key(sheet_id)
        worksheet = spreadsheet.worksheet(_WORKSHEET_TITLE)
        sheet_rows = worksheet.get_all_values()
        aggregates = build_photo_aggregates(metadata_rows)
        author_name_by_user_id = _resolve_discord_usernames(
            agg.latest_author_id for agg in aggregates.values()
        )
        payload = build_catabase_sync_table(
            sheet_rows,
            metadata_rows,
            aggregates=aggregates,
            timezone_name=str(getattr(spreadsheet, "timezone", "") or "").strip() or None,
            author_name_by_user_id=author_name_by_user_id,
        )
        rows = payload["rows"]
        data_rows = int(payload["data_rows"])
        if data_rows <= 0:
            return {"status": "empty_sheet"}

        _write_snapshot(rows)
        if int(payload["changed_rows"]) <= 0:
            return {
                "status": "skipped",
                "rows": data_rows,
                "matched_cats": int(payload["matched_cats"]),
                "changed_rows": 0,
            }

        last_row = data_rows + 1
        worksheet.batch_update(
            [
                {"range": f"C2:E{last_row}", "values": payload["cde_values"]},
                {"range": f"Q2:Q{last_row}", "values": payload["q_values"]},
            ],
            value_input_option="USER_ENTERED",
        )
        log_action(
            "catabase_photo_sync",
            f"rows={data_rows}; matched={int(payload['matched_cats'])}",
            f"changed={int(payload['changed_rows'])}",
        )
        return {
            "status": "ok",
            "rows": data_rows,
            "matched_cats": int(payload["matched_cats"]),
            "changed_rows": int(payload["changed_rows"]),
        }


async def start_catabase_photo_sync_scheduler() -> None:
    """Periodically update CatDatabase derived columns from local photo metadata."""
    if not bool(getattr(settings, "catabase_photo_sync_enabled", True)):
        return
    interval_sec = max(
        30,
        int(getattr(settings, "catabase_photo_sync_interval_sec", 300) or 300),
    )
    while True:
        try:
            await asyncio.to_thread(sync_catabase_photo_columns)
        except Exception as e:
            log_action("catabase_photo_sync_error", "scheduler", str(e))
        await asyncio.sleep(float(interval_sec))
