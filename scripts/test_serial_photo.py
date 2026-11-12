"""Exercise serial-photo routing through delivery using an isolated photo cache.

Run: python scripts/test_serial_photo.py
"""
from __future__ import annotations

import csv
import io
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import _test_support  # noqa: F401
from PIL import Image

from tomcat import intent_router
from tomcat.handlers import cats
from tomcat.services import local_photos


class SerialPhotoTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tomcat-serial-photo-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.root_patch = patch.object(local_photos, "photo_root", return_value=self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        metadata_path = patch.object(local_photos, "metadata_csv_path", return_value=self.root / "photos.csv")
        metadata_path.start()
        self.addCleanup(metadata_path.stop)
        # A serial lookup must work without cat labels, profiles or Google Sheets.
        for name in ("_lookup_cat_profile", "_resolve_random_photo_pick"):
            guard = patch.object(cats, name, new=AsyncMock(side_effect=AssertionError(name)))
            guard.start()
            self.addCleanup(guard.stop)
        aliases = patch.object(intent_router, "alias_vocab", return_value={"cats": [], "stations": [], "all": []})
        aliases.start()
        self.addCleanup(aliases.stop)
        self.router = intent_router.IntentRouter()
        self.channel = SimpleNamespace(
            id=90002, name="test", send=AsyncMock(return_value=SimpleNamespace(id=789))
        )

    def write_metadata(self, *rows):
        with (self.root / "photos.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=local_photos.CSV_HEADERS)
            writer.writeheader()
            writer.writerows(rows)

    def create_photo(self, serial=42):
        Image.new("RGB", (100, 60), "orange").save(self.root / f"sn{serial:04d}.png")
        local_photos._ensure_index(force=True)

    async def request(self, text):
        message = SimpleNamespace(
            id=123, content=text, channel=self.channel,
            author=SimpleNamespace(id=456, name="tester", roles=[]),
            attachments=[], mentions=[], reference=None,
        )
        row = self.router._machine_row_from_message(message)
        event = await self.router._analyze_with_context(row, message)
        self.assertEqual(event.type, "show_photo")
        await self.router._dispatch(event, message, {"channel": self.channel, "message": message})

    async def test_exact_original_photo_without_labels(self):
        for serial, ext, format_name in ((42, ".png", "PNG"), (123456, ".jpg", "JPEG")):
            with self.subTest(serial=serial):
                filename = f"sn{serial:04d}{ext}"
                image_path = self.root / filename
                Image.new("RGB", (100, 60), "orange").save(image_path, format=format_name)
                local_photos._ensure_index(force=True)
                self.channel.send.reset_mock()
                await self.request(f"Tom, show me SN{serial:08d}")
                self.channel.send.assert_awaited_once()
                sent = self.channel.send.call_args.kwargs
                attachment = sent["file"]
                self.assertEqual(attachment.filename, filename)
                self.assertEqual(attachment.fp.read(), image_path.read_bytes())
                self.assertEqual(sent["embed"].image.url, f"attachment://{filename}")
                self.assertIn(f"sn{serial:04d}", sent["embed"].title)
                self.assertNotIn("view", sent)
                self.assertIsNone(sent["embed"].description)
                self.assertEqual(sent["embed"].fields, [])
                attachment.close()

    async def test_origin_labeled_cats_and_original_posting_date(self):
        self.create_photo()
        self.write_metadata(
            {"Serial Number": "sn0041", "Box Cat IDs": "99. Wrong cat"},
            {
                "Serial Number": "SN00042", "Guild ID": "551082419768393729",
                "Channel": "956398865421074462", "Message ID": "1570256127394840597",
                "Discord URL": "https://cdn.discordapp.com/attachments/111/222/photo.png",
                "Timestamp": "2026-11-12T01:57:14.719Z",
                "Box Cat IDs": "1. Microwave|2. Twix|1. microwave|Needs Review|Rejected|0. NotACat|",
            },
        )
        await self.request("tom show me sn42")
        sent = self.channel.send.call_args.kwargs
        self.assertEqual(
            sent["embed"].description,
            "[photo origin](https://discord.com/channels/551082419768393729/956398865421074462/1570256127394840597)",
        )
        fields = {field.name: field.value for field in sent["embed"].fields}
        self.assertEqual(fields["Labeled cats"], "Microwave, Twix")
        posted = int(datetime(2026, 11, 12, 1, 57, 14, tzinfo=timezone.utc).timestamp())
        self.assertEqual(fields["Posted"], f"<t:{posted}:D>")
        sent["file"].close()

    async def test_unlabeled_photo_has_origin_and_date_without_cat_field(self):
        self.create_photo()
        self.write_metadata({
            "Serial Number": "sn0042", "Guild ID": "111", "Channel": "222",
            "Message ID": "1570256127394840597", "Box Cat IDs": "NeedsReview|Rejected|0. NotACat",
        })
        await self.request("tom show me sn42")
        sent = self.channel.send.call_args.kwargs
        self.assertIn("/111/222/1570256127394840597)", sent["embed"].description)
        fields = {field.name: field.value for field in sent["embed"].fields}
        self.assertNotIn("Labeled cats", fields)
        posted = int(datetime(2026, 11, 12, 1, 57, 14, tzinfo=timezone.utc).timestamp())
        self.assertEqual(fields["Posted"], f"<t:{posted}:D>")
        sent["file"].close()

    async def test_attachment_url_is_never_presented_as_message_origin(self):
        self.create_photo()
        self.write_metadata({
            "Serial Number": "42", "Discord URL": "https://cdn.discordapp.com/attachments/111/222/photo.png",
            "Timestamp": "invalid", "Box Cat IDs": "2. Twix",
        })
        await self.request("tom show me sn42")
        sent = self.channel.send.call_args.kwargs
        self.assertIsNone(sent["embed"].description)
        self.assertEqual({field.name: field.value for field in sent["embed"].fields}, {"Labeled cats": "Twix"})
        sent["file"].close()

    async def test_stored_message_link_is_used_when_ids_are_missing(self):
        self.create_photo()
        url = "https://discord.com/channels/111/222/1570256127394840597"
        self.write_metadata({"Serial Number": "sn0042", "Discord URL": url})
        await self.request("tom show me sn42")
        sent = self.channel.send.call_args.kwargs
        self.assertEqual(sent["embed"].description, f"[photo origin]({url})")
        posted = int(datetime(2026, 11, 12, 1, 57, 14, tzinfo=timezone.utc).timestamp())
        self.assertEqual({field.name: field.value for field in sent["embed"].fields}, {"Posted": f"<t:{posted}:D>"})
        sent["file"].close()

    async def test_metadata_failure_does_not_prevent_image_delivery(self):
        self.create_photo()
        with patch.object(local_photos, "read_metadata_rows", side_effect=OSError("unreadable metadata")):
            await self.request("tom show me sn42")
        self.channel.send.assert_awaited_once()
        sent = self.channel.send.call_args.kwargs
        self.assertEqual(sent["file"].filename, "sn0042.png")
        self.assertIsNone(sent["embed"].description)
        sent["file"].close()

    async def test_dm_origin_and_timezone_aware_posting_date(self):
        self.create_photo()
        self.write_metadata({
            "Serial Number": "sn0042", "Channel": "222", "Message ID": "1570256127394840597",
            "Timestamp": "2026-11-11T19:57:14.719-06:00",
        })
        await self.request("tom show me sn42")
        sent = self.channel.send.call_args.kwargs
        self.assertEqual(
            sent["embed"].description,
            "[photo origin](https://discord.com/channels/@me/222/1570256127394840597)",
        )
        posted = int(datetime(2026, 11, 12, 1, 57, 14, tzinfo=timezone.utc).timestamp())
        self.assertEqual({field.name: field.value for field in sent["embed"].fields}, {"Posted": f"<t:{posted}:D>"})
        sent["file"].close()

    async def test_missing_photo_replies_without_an_attachment(self):
        local_photos._ensure_index(force=True)
        await self.request("tom show me sn99999")
        self.channel.send.assert_awaited_once_with(
            "photo sn99999 could not be retrieved"
        )

    async def test_zero_serial_replies_without_an_attachment(self):
        await self.request("tomcat show me sn0")
        self.channel.send.assert_awaited_once_with(
            "photo sn0000 could not be retrieved"
        )

    async def test_serial_photo_uses_existing_attachment_limit(self):
        image_path = self.root / "sn0001.png"
        Image.effect_noise((512, 512), 100).convert("RGB").save(image_path, format="PNG")
        local_photos._ensure_index(force=True)
        limit = 8192
        self.assertGreater(image_path.stat().st_size, limit)
        with patch.object(cats.settings, "discord_attachment_max_bytes", limit):
            await self.request("tom show me sn1")
        attachment = self.channel.send.call_args.kwargs["file"]
        self.assertEqual(attachment.filename, "sn0001.jpg")
        delivered = attachment.fp.read()
        self.assertLessEqual(len(delivered), limit)
        with Image.open(io.BytesIO(delivered)) as image:
            image.verify()
        attachment.close()


if __name__ == "__main__":
    unittest.main()
