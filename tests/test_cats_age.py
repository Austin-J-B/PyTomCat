import sys
import types
import unittest

# Stub dependencies for handler import
class DummyEmbed:
    def __init__(self, *args, **kwargs):
        self.title = kwargs.get('title')
        self.description = kwargs.get('description')
    def set_image(self, *args, **kwargs):
        pass
    def add_field(self, *args, **kwargs):
        pass
    @classmethod
    def from_dict(cls, data):
        return cls()

class DummyFile:
    def __init__(self, *args, **kwargs):
        pass

class DummyView:
    def __init__(self, *args, **kwargs):
        pass

class DummyMessageable:
    pass

def _button(**kwargs):
    def decorator(func):
        return func
    return decorator

ButtonStyle = types.SimpleNamespace(primary=1, secondary=2)

discord_stub = types.ModuleType('discord')
discord_stub.Embed = DummyEmbed
discord_stub.File = DummyFile
ui_stub = types.SimpleNamespace(View=DummyView, button=_button, ButtonStyle=ButtonStyle)
discord_stub.ui = ui_stub
discord_stub.ButtonStyle = ButtonStyle
discord_stub.errors = types.SimpleNamespace(NotFound=Exception)
discord_stub.abc = types.SimpleNamespace(MessageableChannel=DummyMessageable)
setattr(discord_stub, '__path__', [])

sys.modules['discord'] = discord_stub
sys.modules['discord.errors'] = discord_stub.errors

sys.modules.setdefault('aiohttp', types.ModuleType('aiohttp'))

# Pillow stubs
pil_module = types.ModuleType('PIL')
pil_image = types.ModuleType('PIL.Image')
pil_draw = types.ModuleType('PIL.ImageDraw')
pil_font = types.ModuleType('PIL.ImageFont')
pil_module.Image = pil_image
pil_module.ImageDraw = pil_draw
pil_module.ImageFont = pil_font
sys.modules.setdefault('PIL', pil_module)
sys.modules.setdefault('PIL.Image', pil_image)
sys.modules.setdefault('PIL.ImageDraw', pil_draw)
sys.modules.setdefault('PIL.ImageFont', pil_font)

# Torch / vision stubs
torch_module = types.ModuleType('torch')
class _Tensor:
    pass
torch_module.Tensor = _Tensor
torch_module.device = lambda *args, **kwargs: None
sys.modules.setdefault('torch', torch_module)

torchvision_module = types.ModuleType('torchvision')
sys.modules.setdefault('torchvision', torchvision_module)
sys.modules.setdefault('torchvision.transforms', types.ModuleType('torchvision.transforms'))
sys.modules.setdefault('ultralytics', types.ModuleType('ultralytics'))

from tomcat.handlers.cats import _format_age_value


class CatAgeFormattingTests(unittest.TestCase):
    def test_returns_empty_for_blank(self):
        self.assertEqual(_format_age_value(""), "")
        self.assertEqual(_format_age_value(None), "")

    def test_numeric_age_years(self):
        self.assertEqual(_format_age_value(3), "3 years")
        self.assertEqual(_format_age_value(1.2), "1 year")

    def test_numeric_age_months(self):
        self.assertEqual(_format_age_value(0.5), "6 months")
        self.assertEqual(_format_age_value(0.08), "1 month")

    def test_date_string_months(self):
        # Pick a date roughly two months ago
        from datetime import datetime, timedelta, timezone
        two_months_ago = (datetime.now(timezone.utc) - timedelta(days=60)).strftime("%m/%d/%Y")
        formatted = _format_age_value(two_months_ago)
        self.assertTrue("month" in formatted)

    def test_date_string_years(self):
        formatted = _format_age_value("1/1/2010")
        self.assertIn("years", formatted)


if __name__ == "__main__":
    unittest.main()
