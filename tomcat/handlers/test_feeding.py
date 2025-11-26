import unittest

from . import feeding


class ResolveAllowedFeedingChannelsTests(unittest.TestCase):
    def test_includes_feeding_and_sandbox_when_explicit_present(self) -> None:
        allowed = feeding._resolve_allowed_feeding_channels([111, "1341696618688286720"], 222, 333)
        self.assertEqual(allowed, {111, 222, 333, 1341696618688286720})

    def test_returns_empty_when_no_explicit_allowed(self) -> None:
        allowed = feeding._resolve_allowed_feeding_channels([], 222, 333)
        self.assertEqual(allowed, set())

    def test_normalizes_strings_and_ignores_invalid(self) -> None:
        allowed = feeding._resolve_allowed_feeding_channels(["643586809166561310", "bad"], "1341696618688286720", None)
        self.assertEqual(allowed, {643586809166561310, 1341696618688286720})


if __name__ == "__main__":
    unittest.main()
