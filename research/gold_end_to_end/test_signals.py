"""Synthetic checks of frozen rules, IANA clocks and prefix invariance."""

from datetime import datetime
from unittest import TestCase

from research.gold_end_to_end.signals import MINUTE, WARMUP, scan


INTERVAL = 15 * MINUTE


def epoch(value):
    return int(datetime.fromisoformat(value).timestamp() * 1000)


def candle(t, o=100, h=101, low=99, c=100):
    return {"time": t, "time_close": t + INTERVAL, "open": o, "high": h, "low": low, "close": c}


def gap_fixture(t, side, gap_filled):
    before = [candle(opening) for opening in range(t - INTERVAL * (WARMUP + 4), t - 60 * MINUTE, INTERVAL)]
    adverse = 100 if gap_filled else 101
    if side > 0:
        opening = candle(t, o=102, h=104, low=adverse, c=103)
    else:
        opening = candle(t, o=98, h=200 - adverse, low=96, c=97)
    return before + [opening]


def midpoint_fixture(t, side, *, reference_gap=False):
    before = [candle(opening) for opening in range(t - WARMUP * INTERVAL, t, INTERVAL)]
    first_window = [candle(opening) for opening in range(t, t + 6 * 60 * MINUTE, INTERVAL)]
    before.extend(first_window[1:] if reference_gap else first_window)
    quality = [(100, 102, 99, 101), (101, 102, 99, 100), (100, 102, 99, 101), (101, 103, 101, 102),
               (102, 103, 99, 100), (100, 102, 99, 101)]
    for i, (op, high, low, close) in enumerate(quality):
        opening = t + 6 * 60 * MINUTE + i * INTERVAL
        if side == 1:
            before.append(candle(opening, op, high, low, close))
        else:
            before.append(candle(opening, 200 - op, 200 - low, 200 - high, 200 - close))
    return before


class SignalTests(TestCase):
    def test_new_york_reopen_across_dst_and_mirrored_gap_hold(self):
        for opening in ("2026-10-12T22:00:00+00:00", "2026-11-02T23:00:00+00:00"):
            for side in (1, -1):
                t = epoch(opening)
                with self.subTest(opening=opening, side=side):
                    held = scan(gap_fixture(t, side, False), 15)
                    base, = held["G_BASE"]["offers"]
                    hold, = held["G_HOLD"]["offers"]
                    self.assertEqual(hold["side"], side)
                    self.assertEqual(hold["known_ms"], t + INTERVAL)
                    self.assertEqual(hold["reference_known_ms"], t - 60 * MINUTE)
                    self.assertEqual(hold["stop"], base["stop"])
                    filled = scan(gap_fixture(t, side, True), 15)
                    self.assertEqual(len(filled["G_BASE"]["offers"]), 1)
                    self.assertEqual(filled["G_HOLD"]["offers"], [])
                    self.assertEqual([x["stage"] for x in filled["G_HOLD"]["observations"]], ["WATCH"])

    def test_calendar_excludes_weekend_friday_and_abnormal_pause(self):
        friday = epoch("2026-10-16T22:00:00+00:00")
        sunday = epoch("2026-10-18T22:00:00+00:00")
        for t in friday, sunday:
            self.assertEqual(scan(gap_fixture(t, 1, False), 15)["G_HOLD"]["observations"], [])
        monday = epoch("2026-10-12T22:00:00+00:00")
        abnormal = gap_fixture(monday, 1, False)
        del abnormal[-2]
        self.assertEqual(scan(abnormal, 15)["G_HOLD"]["observations"], [])

    def test_midpoint_hold_fails_once_reseeds_and_waits_exactly_one_bar(self):
        t = epoch("2026-11-02T00:00:00+00:00")
        for side in (1, -1):
            with self.subTest(side=side):
                full = midpoint_fixture(t, side)
                tapes = scan(full, 15)
                cross, = tapes["M_CROSS"]["offers"]
                hold, = tapes["M_HOLD"]["offers"]
                self.assertEqual(cross["side"], side)
                self.assertEqual(cross["known_ms"], t + 6 * 60 * MINUTE + INTERVAL)
                self.assertEqual(hold["seed_ms"], cross["known_ms"] + 2 * INTERVAL)
                self.assertEqual(hold["known_ms"], hold["seed_ms"] + INTERVAL)
                self.assertEqual(hold["frozen_level"], 100)
                self.assertEqual(hold["reference_known_ms"], t + 6 * 60 * MINUTE)
                self.assertEqual([x["stage"] for x in tapes["M_HOLD"]["observations"]], ["WATCH", "WATCH", "CONFIRMED"])
                for cut in range(len(full) - 8, len(full) + 1):
                    prefix = scan(full[:cut], 15)
                    self.assertEqual(prefix, {
                        arm: {kind: [x for x in rows if x["index"] < cut] for kind, rows in tape.items()}
                        for arm, tape in tapes.items()
                    })
                damaged = scan(midpoint_fixture(t, side, reference_gap=True), 15)
                self.assertEqual(damaged["M_HOLD"]["observations"], [])
                self.assertEqual(damaged["M_CROSS"]["offers"], [])

    def test_midpoint_pending_hold_cannot_cross_a_gap_or_the_window_end(self):
        day = epoch("2026-11-02T00:00:00+00:00")
        bars = midpoint_fixture(day, 1)[:-3]
        filled = candle(bars[-1]["time_close"], 101, 103, 101, 102)
        self.assertEqual(len(scan(bars + [filled], 15)["M_HOLD"]["offers"]), 1)
        delayed = candle(bars[-1]["time_close"] + INTERVAL, 101, 103, 101, 102)
        self.assertEqual(scan(bars + [delayed], 15)["M_HOLD"]["offers"], [])

        range_only = bars[:-3]
        morning = day + 6 * 60 * MINUTE
        almost_noon = day + 12 * 60 * MINUTE - 2 * INTERVAL
        quiet = [candle(t) for t in range(morning, almost_noon, INTERVAL)]
        seed = candle(almost_noon, 100, 102, 99, 101)
        outside = candle(seed["time_close"], 101, 103, 101, 102)
        observed = scan(range_only + quiet + [seed, outside], 15)
        self.assertEqual([row["stage"] for row in observed["M_HOLD"]["observations"]], ["WATCH"])
        self.assertEqual(observed["M_HOLD"]["offers"], [])
