"""Exercise serial-photo routing through delivery using an isolated photo cache.

Run: python scripts/test_serial_photo.py
"""
from __future__ import annotations

import io
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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
                attachment.close()

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
