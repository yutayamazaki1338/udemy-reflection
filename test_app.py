import unittest
from datetime import datetime, timedelta, timezone

from app import parse_recorded_at


class ParseRecordedAtTests(unittest.TestCase):
    def test_parses_utc_z_suffix_with_milliseconds(self):
        parsed = parse_recorded_at('2026-10-20T09:30:00.000Z')
        self.assertEqual(parsed, datetime(2026, 10, 20, 9, 30, tzinfo=timezone.utc))

    def test_parses_explicit_utc_offset(self):
        parsed = parse_recorded_at('2026-10-20T18:30:00+09:00')
        self.assertEqual(parsed.utcoffset(), timedelta(hours=9))

    def test_rejects_timestamp_without_timezone(self):
        with self.assertRaisesRegex(ValueError, '日時にはタイムゾーンが必要です'):
            parse_recorded_at('2026-10-20T18:30:00')

    def test_rejects_invalid_timestamp(self):
        with self.assertRaises(ValueError):
            parse_recorded_at('not-a-timestamp')


if __name__ == '__main__':
    unittest.main()
