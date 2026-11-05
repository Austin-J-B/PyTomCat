"""Regression coverage for age estimates on both profile paths."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tomcat.services.cat_age import age_estimate_label, format_age_value
from tomcat.services import catsheets
from tomcat.handlers import cats


class AgeEstimateTests(unittest.TestCase):
    def test_age_at_last_sighting_and_birthday_boundaries(self):
        self.assertEqual(format_age_value('3/5/2018', '3/4/2025'), '6 years')
        self.assertEqual(format_age_value('3/5/2018', '3/5/2025'), '7 years')
        self.assertEqual(format_age_value('2024-10-10', '2025-03-09'), '4 months')
        self.assertEqual(age_estimate_label('3/5/2018', '3/4/2025'), 'Age Estimate (at last sighting)')

    def test_missing_invalid_and_future_dates(self):
        self.assertEqual(format_age_value('1/1/2000', ''), format_age_value('1/1/2000', 'invalid'))
        self.assertEqual(format_age_value('1/1/2000', '2999-01-01'), format_age_value('1/1/2000'))
        self.assertEqual(format_age_value('1/1/2020', '1/1/2019'), '1/1/2020')
        self.assertEqual(format_age_value('approximately adult', '1/1/2020'), 'approximately adult')
        self.assertEqual(age_estimate_label('1/1/2020', '1/1/2019'), 'Age Estimate')

    def test_live_sheet_profile_uses_last_seen(self):
        row = [''] * 16
        row[0], row[2], row[9] = '1. Alaska', '3/4/2025', '3/5/2018'
        with patch.object(catsheets.settings, 'sheet_catabase_id', 'test'), patch.object(catsheets, '_read_catabase_rows', return_value=[['header'], row]):
            profile = asyncio.run(catsheets.get_cat_profile('Alaska'))
        self.assertEqual(profile['age'], '6 years')
        with patch.object(catsheets, 'get_cat_profile', new=AsyncMock(return_value=profile)), patch.object(catsheets, 'get_most_recent_photo', new=AsyncMock(return_value=None)):
            embed = asyncio.run(catsheets.build_profile_embed('Alaska'))
        self.assertIn('**Age Estimate (at last sighting):** 6 years', embed['description'])

    def test_cached_profile_card_uses_last_seen(self):
        profile = {'actual_name': 'Alaska', 'birthday_estimate': '3/5/2018', 'last_seen_date': '3/4/2025'}
        channel = SimpleNamespace(send=AsyncMock())
        with patch.object(cats, '_lookup_cat_profile', new=AsyncMock(return_value=profile)), patch.object(cats, '_build_latest_profile_image_payload', new=AsyncMock(return_value=(None, None))):
            asyncio.run(cats.handle_cat_profile(SimpleNamespace(data={'name': 'Alaska'}), {'channel': channel}))
        embed = channel.send.await_args.kwargs['embed']
        self.assertIn('**Age Estimate (at last sighting):** 6 years', embed.description)


if __name__ == '__main__':
    unittest.main()
