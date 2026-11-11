import csv
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from gold_pro.direct_trend_breakout import CONSOLIDATION, RETEST, STRUCTURAL, closed_context, scan
from gold_pro.rc1_qa import EvidenceError
from gold_pro.replay import Costs
from research.direct_trend_breakout.shadow_native import comparison


INTERVAL = 900_000
START = 1_790_000_000_000


def bars_from(rows):
    return [
        dict(time=START + i * INTERVAL, time_close=START + (i + 1) * INTERVAL,
             open=opening, high=high, low=low, close=close)
        for i, (opening, high, low, close) in enumerate(rows)
    ]


def structural_case(break_bar=(100.8, 101.1, 98.4, 98.75), later=(99.6, 99.90, 98.25, 98.50)):
    seed = [(103., 104., 102., 103.)] * 24
    return bars_from(seed + [
        (103., 105., 102., 103.6), (103.6, 104., 102.4, 103.1), (103.1, 103.4, 101.9, 102.7),
        (102.7, 103., 99.8, 101.9), (101.9, 103.2, 101.1, 102.4), (102.4, 103.7, 101.6, 103.1),
        (103.1, 104.2, 102.1, 103.), (103., 103.3, 100.9, 102.), (102., 102.4, 99.9, 100.5),
        (100.5, 102., 100.1, 100.35), break_bar, later,
    ])


def write_fixture_export(path, bars, *, change_timestamp=False, change_value=False, duplicate=None):
    events, _ = scan(bars)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(("Date", "Message"))
        for index, bar in enumerate(bars):
            batch = ["DT_MARKET|" + json.dumps(dict(bar, symbol="OANDA:XAUUSD", timeframe=15, index=index))]
            for event in events:
                if event["bar"] != index:
                    continue
                record = dict(event)
                if change_timestamp:
                    record["time_ms"] -= INTERVAL
                    change_timestamp = False
                if change_value:
                    record["level"] += .01
                    change_value = False
                batch.append("DT_SHADOW|" + json.dumps(record))
            writer.writerows(("fixture", item) for item in batch)
            if duplicate is not None and index == len(bars) - 1:
                writer.writerows(("fixture", item) for item in (batch if duplicate else batch[:-1]))
        writer.writerow(("fixture", "DT_END|" + json.dumps(
            {"index": len(bars) - 1, "time_close": bars[-1]["time_close"]})))


class DirectTrendBreakoutTest(unittest.TestCase):
    def test_strict_pivot_publication_and_closed_history_prefix(self):
        bars = structural_case()
        context = list(closed_context(bars))
        floor = context[29].confirmed_low
        self.assertIsNone(context[27].confirmed_low)
        self.assertEqual((floor.price, floor.source_ms, floor.known_ms),
                         (99.8, bars[27]["time"], bars[29]["time_close"]))
        self.assertNotIn(floor, context[29].known_lows)
        self.assertIn(floor, context[30].known_lows)
        events, plans = scan(bars)
        for length in range(len(bars) + 1):
            prior_events, prior_plans = scan(bars[:length])
            self.assertEqual(prior_events, [event for event in events if event["bar"] < length])
            self.assertEqual(prior_plans, [plan for plan in plans if plan.bar < length])

    def test_mirrored_watch_break_retest_and_geometry(self):
        bars = structural_case()
        sell, sell_plans = scan(bars, enabled={STRUCTURAL, RETEST})
        self.assertEqual([(x["bar"], x["state"]) for x in sell], [
            (33, "WATCH"), (34, "BREAKOUT CONFIRMED"), (34, "ENTRY CANDIDATE"),
            (34, "RETEST WATCH"), (35, "BREAKOUT CONFIRMED"), (35, "ENTRY CANDIDATE"),
        ])
        self.assertEqual([(p.model, p.side, p.bar) for p in sell_plans],
                         [(STRUCTURAL, -1, 34), (RETEST, -1, 35)])
        self.assertEqual(sell[1]["watch_ms"], bars[33]["time_close"])
        self.assertEqual(sell[-1]["breakout_ms"], bars[34]["time_close"])
        self.assertEqual(sell[2]["plan_id"], sell_plans[0].id)
        buy_bars = [dict(b, open=210-b["open"], high=210-b["low"], low=210-b["high"],
                         close=210-b["close"]) for b in bars]
        buy, buy_plans = scan(buy_bars, enabled={STRUCTURAL, RETEST})
        self.assertEqual([(x["bar"], x["side"], x["state"]) for x in sell],
                         [(x["bar"], -x["side"], x["state"]) for x in buy])
        for a, b in zip(sell_plans, buy_plans, strict=True):
            for x, y in zip((a.reference, a.stop, *a.targets), (b.reference, b.stop, *b.targets), strict=True):
                self.assertAlmostEqual(x + y, 210)

    def test_retest_can_be_measured_independently_and_invalidates_on_reclaim(self):
        bars = structural_case(later=(98.85, 101.5, 98.4, 100.75))
        events, plans = scan(bars, enabled={RETEST})
        self.assertEqual(plans, [])
        self.assertEqual([(x["state"], x["reason"]) for x in events], [
            ("RETEST WATCH", "CONFIRMED_BREAK_LEVEL"), ("INVALIDATED", "CLOSE_BACK_THROUGH_BREAK_LEVEL"),
        ])

    def test_price_gap_expires_watch_and_large_move_is_risk_rejected(self):
        bars = structural_case(break_bar=(100.8, 101.1, 95.8, 96.0))
        events, plans = scan(bars[:35], enabled={STRUCTURAL})
        self.assertEqual(plans, [])
        self.assertEqual([x["state"] for x in events], ["WATCH", "BREAKOUT CONFIRMED", "REJECTED"])
        self.assertIn("LATE_EXTENSION", events[-1]["reasons"])
        self.assertIn("STOP_TOO_WIDE", events[-1]["reasons"])
        broken = bars[34]
        separated = dict(broken, time=broken["time"] + 4 * INTERVAL, time_close=broken["time_close"] + 4 * INTERVAL)
        events, plans = scan(bars[:34] + [separated], enabled={STRUCTURAL})
        self.assertEqual(plans, [])
        self.assertEqual([(x["state"], x["reason"]) for x in events],
                         [("WATCH", "APPROACHING_LEVEL"), ("EXPIRED", "DATA_GAP")])

    def test_consolidation_needs_only_prior_box_and_trend(self):
        trend = [(112 - .25*i, 112.9 - .25*i, 111.1 - .25*i, 111.85 - .25*i) for i in range(30)]
        bars = bars_from(trend + [
            (104.60, 105.10, 104.45, 104.75), (104.75, 105.00, 104.43, 104.80),
            (104.80, 105.05, 104.41, 104.77), (104.77, 105.00, 104.40, 104.58),
            (104.58, 104.80, 102.90, 103.10),
        ])
        events, plans = scan(bars, Costs(spread=.1, commission=.05), enabled={CONSOLIDATION})
        self.assertEqual([x["state"] for x in events[-3:]], ["WATCH", "BREAKOUT CONFIRMED", "ENTRY CANDIDATE"])
        self.assertEqual(len(plans), 1)
        self.assertEqual((plans[0].side, plans[0].bar), (-1, 34))
        self.assertEqual(events[-1]["level_known_ms"], bars[33]["time_close"])
        self.assertEqual(events[-1]["level"], 104.40)

    def test_native_checker_flags_drift_and_requires_known_at_close(self):
        bars = structural_case()
        with TemporaryDirectory() as temp:
            baseline = Path(temp) / "export.csv"
            repeat = Path(temp) / "repeat.csv"
            write_fixture_export(baseline, bars)
            write_fixture_export(repeat, bars)
            exact = comparison(baseline, repeat)
            self.assertEqual(exact["status"], "MATCH")
            self.assertEqual(exact["native_repeat"]["status"], "MATCH")
            self.assertEqual(exact["python_prefix_consistency"]["status"], "MATCH")
            write_fixture_export(repeat, bars, duplicate=True)
            deduplicated = comparison(baseline, repeat)
            self.assertEqual(deduplicated["native_repeat"]["status"], "MATCH")
            self.assertEqual(comparison(repeat)["repeated_identical_confirmed_batches"][0]["events"], 2)
            write_fixture_export(repeat, bars, duplicate=False)
            with self.assertRaisesRegex(EvidenceError, "inconsistent repeated confirmed batch"):
                comparison(repeat)
            write_fixture_export(repeat, bars, change_value=True)
            self.assertEqual(comparison(repeat)["first_difference"]["field"], "level")
            self.assertEqual(comparison(baseline, repeat)["native_repeat"]["status"], "MISMATCH")
            write_fixture_export(repeat, bars, change_timestamp=True)
            with self.assertRaisesRegex(EvidenceError, "event not logged after its confirmed market bar"):
                comparison(repeat)


if __name__ == "__main__":
    unittest.main()
