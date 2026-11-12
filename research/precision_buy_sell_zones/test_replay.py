"""Targeted causal-zone and native-parser regression checks on synthetic candles."""

import csv
from dataclasses import asdict
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from .native_qa import event_payload, frame_report, utc
from .replay import (EXPIRED, FORMED, INVALIDATED, PARTIAL, REJECTION, TOUCH, ActiveZone, Pivot, Zone,
                     advance, locate_origin, read_protocol, replay, update_pivots)


START = 1794355200000
STEP = 300_000
RULES = read_protocol()


def candle(index, open_=100., high=100.2, low=99.8, close=100., *, offset=0):
    opened = START + (index + offset) * STEP
    return dict(time=opened, time_close=opened + STEP, open=open_, high=high, low=low, close=close)


def buy_tape():
    structure = [(100., 101., 99.9, 100.6), (100.6, 102., 100.2, 101.5),
                 (101.5, 101.7, 100.9, 101.1), (101.1, 101.2, 100.7, 100.8),
                 (100.8, 100.9, 100.1, 100.4), (100.4, 102.4, 100.3, 102.3),
                 (100.7, 101.3, 100.3, 101.2), (100.4, 100.5, 99.6, 99.7)]
    return [candle(i) for i in range(250)] + [candle(i, *values) for i, values in enumerate(structure, 250)]


class ZoneReplayTest(unittest.TestCase):
    def test_formation_is_causal_and_lifecycle_survives_prefixes(self):
        bars = buy_tape()
        run = replay(bars, 5)
        formation = next(z for z in run.zones if z.side == 1)
        self.assertEqual((formation.formed_index, formation.pivot_known_ms, formation.break_level),
                         (255, bars[254]["time_close"], 102.))
        self.assertEqual((formation.origin_open_ms, formation.proximal, formation.distal),
                         (bars[254]["time"], 100.8, 100.1))
        events = [e for e in run.events if e.zone_id == formation.id]
        self.assertEqual([e.state for e in events], [FORMED, TOUCH, PARTIAL, REJECTION, INVALIDATED])
        self.assertEqual([e.bar_index for e in events], [255, 256, 256, 256, 257])
        self.assertGreater(events[3].atr, 0)
        self.assertEqual(run.snapshots[255].buy.status, "UNTESTED")
        self.assertEqual(run.snapshots[256].buy.status, REJECTION)
        self.assertIsNone(run.snapshots[257].buy)
        for cut in range(250, len(bars)):
            prefix = replay(bars[:cut + 1], 5)
            self.assertEqual(prefix.events, tuple(e for e in run.events if e.bar_index <= cut))
            self.assertEqual(prefix.snapshots, run.snapshots[:cut + 1])

    def test_sell_is_an_exact_price_mirror(self):
        bars = buy_tape()
        mirrored = [dict(row, open=200 - row["open"], high=200 - row["low"],
                         low=200 - row["high"], close=200 - row["close"]) for row in bars]
        buy_run, sell_run = replay(bars, 5), replay(mirrored, 5)
        self.assertEqual([(z.side, z.known_ms, z.origin_open_ms) for z in buy_run.zones],
                         [(-z.side, z.known_ms, z.origin_open_ms) for z in sell_run.zones])
        self.assertEqual([(e.state, e.known_ms) for e in buy_run.events],
                         [(e.state, e.known_ms) for e in sell_run.events])

    def test_pivot_chooses_rightmost_equal_extremum(self):
        bars = [candle(i, high=102. if i in (3, 4) else 101.) for i in range(8)]
        pivots = {}
        update_pivots(bars, 6, pivots, RULES["zone_rule"])
        self.assertNotIn(1, pivots)
        update_pivots(bars, 7, pivots, RULES["zone_rule"])
        self.assertEqual(pivots[1], Pivot(102., bars[7]["time_close"]))

    def test_origin_search_rejects_wide_nearest_and_never_crosses_gap(self):
        bars = [candle(0, 100., 100.2, 99.8, 99.9), candle(1, 100.5, 100.6, 95., 99.),
                candle(2, 100., 102., 99.9, 101.9)]
        self.assertEqual(locate_origin(bars, 2, 1, .5, .001, RULES["zone_rule"]), (START, 100., 99.8))
        bars[1]["time"] += STEP
        self.assertIsNone(locate_origin(bars, 2, 1, .5, .001, RULES["zone_rule"]))

    def test_gap_through_does_not_touch_and_expiry_precedes_other_events(self):
        zone = Zone("test", 1, START - STEP, START + STEP, 0, 100., 99., 102., START, 1., False)
        previous = candle(0)
        gap = candle(1, 98.5, 98.7, 97.8, 98., offset=1)
        cases = [(1, INVALIDATED), (24, EXPIRED)]
        for index, expected in cases:
            emitted = []
            active = ActiveZone(zone)
            self.assertFalse(advance(active, gap, previous, index, 1., RULES["lifecycle"],
                                     lambda _, state, reason: emitted.append(state)))
            self.assertEqual(emitted, [expected])
            self.assertIsNone(active.first_touch_ms)

    def test_zero_range_has_no_formation(self):
        bars = [candle(i, 100., 100., 100., 100.) for i in range(270)]
        self.assertEqual(replay(bars, 5).events, ())

    def test_parser_and_comparator_check_a_synthetic_journal(self):
        bars = buy_tape()
        expected = replay(bars, 5)
        zones = {zone.id: zone for zone in expected.zones}
        with TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic-test.csv"
            with path.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(["Date", "Message"])
                for i, row in enumerate(bars):
                    date = utc(row["time"])
                    payload = dict(row, symbol=RULES["symbol"], timeframe=5, index=i, tick=.001,
                                   **asdict(expected.inputs[i]))
                    writer.writerow([date, "PZ_MARKET|" + json.dumps(payload)])
                    intents = []
                    for event in expected.events:
                        if event.bar_index == i:
                            details = event_payload(event, zones[event.zone_id])
                            writer.writerow([date, "PZ_EVENT|" + json.dumps(details)])
                            if details["notification"]:
                                intents.append(details)
                    if intents:
                        writer.writerow([date, "PZ_ALERT_INTENT|" + json.dumps({
                            "schema": "qirvanta.precision-zones.research.v1", "symbol": RULES["symbol"],
                            "timeframe": "5", "known_ms": row["time_close"], "events": intents})])
                writer.writerow([utc(bars[-1]["time"]), "PZ_END|" + json.dumps(
                    {"index": len(bars) - 1, "time_close": bars[-1]["time_close"]})])
            report = frame_report(path, 5, RULES)
        self.assertEqual(report["event_parity"]["status"], "MATCH")
        self.assertEqual(report["market_input_parity"]["status"], "MATCH")
        self.assertEqual(report["prefix_invariance"]["status"], "MATCH")
        self.assertEqual(report["alert_intents"]["status"], "MATCH")


if __name__ == "__main__":
    unittest.main()
