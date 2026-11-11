import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from unified.tools.audit_gold_context import ContextRow, audit_rows
from unified.tools.gold_market_data import (
    Bar, HtfContext, NEW_YORK, RollingFingerprint, SessionRanges, active_sessions,
    bucket_bounds, context_rows, finite_emas, htf_side,
)


def ms(raw):
    return int(datetime.fromisoformat(raw).timestamp() * 1000)


def chart(start, count, interval=300_000):
    for i in range(count):
        opened = start + i * interval
        yield Bar(opened, opened + interval, 4010.125 + i / 1000, 4010.505 + i / 1000,
                  4009.955 + i / 1000, 4010.240 + i / 1000)


def traded_m30():
    start = datetime(2026, 1, 4, 23, tzinfo=timezone.utc)
    for i in range(6000):
        opened = start + timedelta(minutes=30 * i)
        local = opened.astimezone(NEW_YORK)
        day, hour = local.weekday(), local.hour
        available = day in (0, 1, 2, 3) and hour != 17 or day == 4 and hour < 17 or day == 6 and hour >= 18
        if available:
            price = 4000 + i / 100
            stamp = int(opened.timestamp() * 1000)
            yield Bar(stamp, stamp + 1_800_000, price, price + 0.5, price - 0.5, price + 0.1)


class GoldMarketDataTests(unittest.TestCase):
    def test_finite_ema_golden_and_missing_history(self):
        fast, slow = finite_emas(list(range(100, 210)))
        self.assertAlmostEqual(fast, 199.0003077494729, places=10)
        self.assertAlmostEqual(slow, 182.51261184961956, places=10)
        self.assertEqual(finite_emas(list(range(100, 209))), (None, None))
        self.assertEqual(finite_emas([None] + list(range(101, 210))), (None, None))

    def test_fingerprint_golden_and_shift(self):
        rows = list(chart(1792726800000, 201))
        fp = RollingFingerprint(0.001)
        actual = [fp.add(row) for row in rows]
        self.assertEqual(actual[:199], [None] * 199)
        self.assertEqual(actual[199:], [(79563159, 500546686), (79810359, 173828573)])
        shorter = RollingFingerprint(0.001)
        self.assertEqual([shorter.add(row) for row in rows[1:]][-1], actual[-1])
        changed = RollingFingerprint(0.001)
        candidate = replace(rows[-1], close=rows[-1].close + .001)
        self.assertNotEqual([changed.add(row) for row in rows[:-1] + [candidate]][-1], actual[-1])

    def test_h4_and_daily_daylight_boundaries(self):
        cases = [
            ("2026-10-26T22:30:00+00:00", "2026-10-26T22:00:00+00:00", "2026-10-27T01:00:00+00:00"),
            ("2026-11-02T23:30:00+00:00", "2026-11-02T23:00:00+00:00", "2026-11-03T02:00:00+00:00"),
            ("2026-11-03T05:30:00+00:00", "2026-11-03T02:00:00+00:00", "2026-11-03T06:00:00+00:00"),
        ]
        for stamp, start, end in cases:
            self.assertEqual(bucket_bounds(ms(stamp), 240), (ms(start), ms(end)))
        self.assertEqual(bucket_bounds(ms("2026-11-03T06:00:00+00:00"), 1440),
                         (ms("2026-11-02T23:00:00+00:00"), ms("2026-11-03T22:00:00+00:00")))

    def test_sessions_use_their_own_clocks_and_flag_partial_data(self):
        self.assertNotIn("NEW YORK", active_sessions(ms("2026-03-06T12:00:00+00:00")))
        self.assertIn("LONDON–NEW YORK", active_sessions(ms("2026-03-20T12:00:00+00:00")))
        self.assertNotIn("LONDON", active_sessions(ms("2026-03-27T07:30:00+00:00")))
        self.assertIn("LONDON", active_sessions(ms("2026-03-30T07:30:00+00:00")))
        begin = ms("2026-03-20T12:00:00+00:00")
        first, middle, third = chart(begin, 3)
        tracker = SessionRanges()
        label, session = tracker.add(first)
        self.assertEqual(label, "LONDON–NEW YORK")
        self.assertTrue(session.complete_to_current)
        _, session = tracker.add(third)
        self.assertFalse(session.complete_to_current)
        self.assertEqual(session.observed_since, begin)
        self.assertFalse(SessionRanges().add(middle)[1].complete_to_current)

    def test_bounded_history_converges_and_prefix_is_immutable(self):
        bars = list(traded_m30())
        full = list(context_rows(bars))
        bounded = list(context_rows(bars[1300:]))
        self.assertFalse(full[0].ready)
        self.assertTrue(full[-1].ready)
        self.assertEqual(full[:1500], list(context_rows(bars[:1500])))
        for old, short in zip(full[-300:], bounded[-300:]):
            self.assertEqual((old.h1, old.h4, old.fingerprint), (short.h1, short.h4, short.fingerprint))
            self.assertLessEqual(old.h4.known_at, old.bar.open_at)
        self.assertEqual(list(context_rows(bars[1300:], earlier=bars[:1300]))[-1].h4, full[-1].h4)

    def test_missing_intrabar_resets_warmup(self):
        bars = list(traded_m30())
        drop = next(i for i, bar in enumerate(bars[2000:], 2000)
                    if bar.open_at % 3_600_000 == 1_800_000)
        complete = list(context_rows(bars[:drop + 5]))
        self.assertTrue(complete[-1].ready)
        incomplete = list(context_rows(bars[:drop] + bars[drop + 1:drop + 5]))
        self.assertFalse(incomplete[-1].ready)
        self.assertIsNone(incomplete[-1].h1.side)

    def test_stale_or_unconfirmed_is_distinct_from_neutral(self):
        self.assertEqual(htf_side(0, 3_600_000, 4000, 4000, 4000, 3_600_000, 60), 0)
        self.assertIsNone(htf_side(0, 3_600_000, 4020, 4000, 3980, 3_599_999, 60))
        self.assertIsNone(htf_side(0, 3_600_000, 4020, 4000, 3980, 10_800_001, 60))
        self.assertIsNone(htf_side(0, 3_600_000, 4020, None, None, 3_600_000, 60))

    def test_context_log_schema_rejects_future_data(self):
        start = ms("2026-03-20T12:00:00+00:00")
        end = start + 300_000
        day = ms("2026-03-19T22:00:00+00:00")
        groups = "|".join(["-"] * 18)
        message = (f"GOLD_CTX|1|OANDA:XAUUSD|5|0.001|{start}|{end}|4000|4001|3999|4000"
                   f"|-|-|0|LONDON–NEW YORK|4001|3999|{start}|1|{day}|4000|4001|3999|0|{groups}")
        row = ContextRow.parse(message)
        self.assertEqual(audit_rows([row])["unknown_context_bars"], 1)
        future = replace(row, htf=(HtfContext(start, end, 4000, 3999, 3998, 1), *row.htf[1:]))
        with self.assertRaisesRegex(ValueError, "Future"):
            audit_rows([future])


if __name__ == "__main__":
    unittest.main()
