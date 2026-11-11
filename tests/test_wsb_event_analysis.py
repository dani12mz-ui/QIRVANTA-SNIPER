import csv
from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from research.wsb_missed_sell import event_analysis as wsb


M5_SIGNAL_OPEN = wsb.visual_signal_open(5)


def candle(stamp, opening, high, low, close):
    return wsb.Bar(stamp, stamp + wsb.BAR_MS, *(Decimal(v) for v in (opening, high, low, close)))


def original_context(bar):
    fields = {
        "symbol": wsb.SYMBOL, "timeframe": str(bar.minutes),
        "open_ms": bar.opening, "close_ms": bar.closing,
        "open": str(bar.open), "high": str(bar.high), "low": str(bar.low), "close": str(bar.close),
    }
    return "WSB_ORIGINAL_CTX|" + json.dumps(fields)


def write_logs(path, messages):
    with path.open("w", encoding="utf-8", newline="") as out:
        writer = csv.DictWriter(out, fieldnames=("Date", "Message"))
        writer.writeheader()
        writer.writerows({"Date": "", "Message": message} for message in messages)


class WsbEventAnalysisTests(unittest.TestCase):
    def test_capture_anchor_and_visible_current_candle_exclusion(self):
        self.assertEqual(wsb.utc(M5_SIGNAL_OPEN), "2026-11-11T13:45:00Z")
        self.assertEqual(wsb.utc(wsb.visual_signal_open(15)), "2026-11-11T09:15:00Z")
        self.assertEqual(wsb.utc(wsb.SCREEN_BAR_OPEN), "2026-11-11T15:55:00Z")
        bars = {}
        for offset, direction in wsb.BODY_DIRECTIONS.items():
            stamp = M5_SIGNAL_OPEN + offset * wsb.BAR_MS
            opening = wsb.ENTRY - direction
            bars[stamp] = candle(stamp, opening, opening + 2, opening - 2, wsb.ENTRY)
        found = wsb.candidate(bars, bars[M5_SIGNAL_OPEN])
        self.assertEqual(found["classification"], "MARKET_CANDLE_MATCH")
        self.assertEqual(found["candle_body_match_count"], len(wsb.BODY_DIRECTIONS))
        self.assertEqual(found["close_utc"], "2026-11-11T13:50:00Z")
        self.assertEqual(wsb.market_analysis(bars, bars[M5_SIGNAL_OPEN].closing)["status"],
                         "INCOMPLETE_CLOSED_CANDLES")

    def test_price_only_match_does_not_establish_a_signal(self):
        stamp = wsb.DAY_OPEN + 565 * 60_000
        bar = candle(stamp, "4049.640", "4050.870", "4049.040", "4049.570")
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "archived.csv"
            write_logs(path, [original_context(bar)])
            result = wsb.report([path])
        self.assertEqual(result["candidates"][0]["close_utc"], "2026-11-11T09:30:00Z")
        self.assertEqual(result["status"], "INSUFFICIENT_MARKET_EVIDENCE")
        self.assertIsNone(result["case_execution"])

    def test_native_plan_can_match_tick_rounded_prices(self):
        opening = wsb.visual_signal_open(15)
        bar = wsb.Bar(opening, opening + wsb.FRAME_MS[15], *(Decimal(v) for v in ("4051", "4053", "4049", "4049.57")))
        plan = {
            "open_ms": bar.opening, "close_ms": bar.closing, "direction": "sell",
            "entry": "4049.5700", "sl": "4058.2308", "tp1": "4040.9092",
            "tp2": "4032.2484", "tp3": "4023.5876",
        }
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "original.csv"
            write_logs(path, [original_context(bar), "WSB_ORIGINAL_PLAN|" + json.dumps(plan)])
            result = wsb.report([path], original_paths=[path])
        self.assertEqual(result["status"], "NATIVE_HISTORICAL_REPRODUCTION")
        self.assertEqual(result["reconciled_signal_timeframe_minutes"], 15)
        self.assertEqual(result["native_wsb_matching_plans"][0]["bar_close_utc"], "2026-11-11T09:30:00Z")
        self.assertEqual(result["case_execution"]["status"], "INCOMPLETE_CLOSED_CANDLES")

    def test_provider_sources_must_agree_on_actual_ohlc(self):
        bar = candle(M5_SIGNAL_OPEN, "4051", "4053", "4049", "4049.57")
        other = candle(M5_SIGNAL_OPEN, "4051", "4053", "4049", "4050.57")
        with TemporaryDirectory() as temporary:
            first = Path(temporary) / "first.csv"
            second = Path(temporary) / "second.csv"
            write_logs(first, [original_context(bar)])
            write_logs(second, [original_context(other)])
            with self.assertRaisesRegex(ValueError, "price conflict"):
                wsb.align_sources([first, second])

    def test_stop_first_ambiguous_candle_never_fills_later_targets(self):
        bar = candle(M5_SIGNAL_OPEN + wsb.BAR_MS, "4049", "4059", "4020", "4030")
        result = wsb.simulate_short([bar], wsb.COSTS[0])
        self.assertEqual([fill["reason"] for fill in result["fills"]], ["SL", "SL", "SL"])
        self.assertEqual(result["ambiguity_stop_first_bar_opens"], [wsb.utc(bar.opening)])
        self.assertLess(result["realized_net_usd"], 0)

    def test_runner_is_same_total_size_and_only_tightens_after_tp3_bar(self):
        stamp = M5_SIGNAL_OPEN + wsb.BAR_MS
        first = candle(stamp, "4049", "4057", "4020", "4025")
        next_bar = candle(first.closing, "4025", "4034", "4018", "4033")
        normal = wsb.simulate_short([first, next_bar], wsb.COSTS[0])
        runner = wsb.simulate_short([first, next_bar], wsb.COSTS[0], runner=True)
        self.assertEqual(normal["allocated_units"], runner["allocated_units"])
        self.assertEqual([fill["reason"] for fill in runner["fills"]], ["TP1", "TP2", "TP3", "SL"])
        self.assertEqual(runner["fills"][-1]["bar_open_utc"], wsb.utc(next_bar.opening))
        self.assertEqual(runner["fills"][-1]["price"], wsb.TARGETS[1] + wsb.COSTS[0].slippage)
        self.assertEqual(runner["fills"][-1]["quantity"], Decimal("0.75"))

    def test_screenshot_cutoff_uses_only_completed_candles(self):
        confirmation = wsb.SCREEN_BAR_OPEN - wsb.BAR_MS
        last = candle(confirmation, "4049", "4050", "4040", "4044")
        future = candle(wsb.SCREEN_BAR_OPEN, "4044", "4045", "3900", "3901")
        result = wsb.market_analysis({last.opening: last, future.opening: future}, confirmation)
        self.assertEqual(result["completed_bars_after_confirmation"], 1)
        self.assertEqual(result["last_completed_close_utc"], "2026-11-11T15:55:00Z")
        self.assertEqual(result["raw_market_mfe_from_reference"], wsb.ENTRY - Decimal("4040"))
        self.assertIsNone(result["raw_target_touches"][1])

    def test_m15_aggregation_requires_all_three_ohlc_constituents(self):
        stamp = wsb.visual_signal_open(15)
        first = candle(stamp, "4053.89", "4053.94", "4051.1", "4052.78")
        second = candle(first.closing, "4052.79", "4053", "4048.465", "4049.64")
        third = candle(second.closing, "4049.64", "4050.87", "4049.04", "4049.57")
        self.assertFalse(wsb.aggregate_m15({b.opening: b for b in (first, third)}))
        result = wsb.aggregate_m15({b.opening: b for b in (first, second, third)})
        self.assertEqual(result[stamp].evidence(), {
            "open_utc": "2026-11-11T09:15:00Z", "close_utc": "2026-11-11T09:30:00Z",
            "timeframe_minutes": 15, "open": Decimal("4053.89"), "high": Decimal("4053.94"),
            "low": Decimal("4048.465"), "close": Decimal("4049.57"),
        })


if __name__ == "__main__":
    unittest.main()
