"""Synthetic protocol tests. None of the fixtures are market evidence."""

import unittest
from math import ceil

from research.donchian_20_10.fine_audit import audit_finer
from research.donchian_20_10.metrics import summarize
from research.donchian_20_10.replay import (
    ADVERSE,
    BASE,
    HOUR_MS,
    INITIAL_CAPITAL,
    Bar,
    Costs,
    Instrument,
    rollovers,
    run,
    utc_ms,
    verify_prefixes,
)


EPOCH = utc_ms("2024-01-01T00:00:00Z")
STEP = HOUR_MS * 4
SYMBOL = Instrument(0.001, 1.0, 1.0)
FREE = Costs("test-only", 0.0, 0, 0.0, 0.0)


def bar(i, opening=100.0, high=101.0, low=99.0, close=100.0, gap=0):
    start = EPOCH + i * STEP + gap
    return Bar(start, start + STEP, opening, high, low, close)


def narrow_seed():
    return [bar(i) for i in range(100)]


def find_event(result, tag):
    return [event.data for event in result.events if event.tag == tag]


class DonchianReplayTests(unittest.TestCase):
    def test_prior_channel_atr_frozen_next_open_and_linked_entry_bar_stop(self):
        tape = [*narrow_seed(), bar(100, 100, 111, 99, 110), bar(101, 113, 119, 106, 118),
                bar(102, 118, 121, 114, 120), bar(103, 121, 123, 118, 122)]
        replay = run(tape, SYMBOL, native=True)
        signals = find_event(replay, "D20_SIGNAL")
        first, second = signals
        self.assertEqual(first["time_ms"], tape[100].close_ms)
        self.assertEqual(first["entry_level"], 101)
        self.assertAlmostEqual(first["atr"], 2.5)
        self.assertEqual(first["stop_ticks"], ceil(2 * first["atr"] / SYMBOL.tick))
        self.assertEqual(second["bar"], 102)
        closed, = replay.trades
        self.assertEqual(closed.entry_bar, closed.exit_bar)
        self.assertEqual(closed.entry_ms, tape[101].time_ms)
        self.assertEqual(closed.reason, "SL")
        self.assertAlmostEqual(closed.entry, 113.002)
        self.assertAlmostEqual(closed.stop, 108.002)
        self.assertAlmostEqual(closed.exit, 108.000)
        self.assertAlmostEqual(closed.holding_hours, 0.0)
        self.assertAlmostEqual(closed.max_holding_hours, 4.0)
        self.assertAlmostEqual(closed.commission_usd, closed.qty * 0.35)
        self.assertEqual(closed.spread_usd, 0.0)
        self.assertEqual(closed.financing_usd, 0.0)
        self.assertIsNotNone(replay.open_trade)

    def test_no_trigger_on_equal_previous_high_and_short_is_symmetric(self):
        tape = [*narrow_seed(), bar(100, 100, 104, 99, 101), bar(101, 100, 101, 87, 88),
                bar(102, 85, 93, 84, 90)]
        result = run(tape, SYMBOL, native=True)
        signal, = find_event(result, "D20_SIGNAL")
        self.assertEqual(signal["bar"], 101)
        self.assertEqual(signal["side"], -1)
        self.assertEqual(signal["entry_level"], 99)
        trade, = result.trades
        self.assertEqual(trade.reason, "SL")
        self.assertEqual(trade.entry_bar, trade.exit_bar)
        self.assertAlmostEqual(trade.entry, 84.998)
        self.assertGreater(trade.stop, trade.entry)
        self.assertAlmostEqual(trade.exit, trade.stop + 0.002)

    def channel_tape(self, final_open):
        tape = [bar(i, 100, 120, 80, 100) for i in range(100)]
        tape += [bar(100, 119, 132, 117, 125)]
        tape += [bar(i, 125, 129, 112, 126) for i in range(101, 111)]
        tape += [bar(111, 126, 128, 100, 104)]
        tape += [bar(112, final_open, max(final_open, 109), min(final_open, 102), 103)]
        return tape

    def test_channel_exit_uses_prior_ten_and_next_open(self):
        tape = self.channel_tape(105)
        result = run(tape, SYMBOL, FREE, native=True)
        channel, = find_event(result, "D20_CHANNEL")
        self.assertEqual(channel["exit_level"], 112)
        self.assertEqual(channel["time_ms"], tape[111].close_ms)
        trade, = result.trades
        self.assertEqual(trade.exit, 105)
        self.assertEqual(trade.exit_ms, tape[112].time_ms)
        self.assertEqual(trade.reason, "CHANNEL")
        self.assertEqual(len(find_event(result, "D20_SIGNAL")), 1)

    def test_native_half_tick_ohlc_rounds_up_with_signed_slippage(self):
        tape = self.channel_tape(105.4375)
        tape[101] = bar(101, 125.6005, 129, 112, 126)
        trade, = run(tape, SYMBOL, BASE, native=True).trades
        self.assertEqual(trade.entry, 125.603)
        self.assertEqual(trade.exit, 105.436)
        self.assertAlmostEqual(trade.net_usd, (trade.exit - trade.entry) * trade.qty - trade.commission_usd)
        self.assertAlmostEqual(trade.net_usd, trade.gross_usd - trade.slippage_usd - trade.commission_usd)

    def test_finer_audit_keeps_coarse_pnl_and_identifies_later_gap(self):
        tape = [*narrow_seed(), bar(100, 100, 111, 99, 110), bar(101, 113, 119, 106, 118)]
        parent = tape[101]
        m30 = [Bar(parent.time_ms + HOUR_MS, parent.time_ms + 3 * HOUR_MS // 2, 113, 115, 109, 110),
               Bar(parent.time_ms + 3 * HOUR_MS // 2, parent.time_ms + 2 * HOUR_MS, 106, 119, 106, 118)]
        result = run(tape, SYMBOL, native=True)
        evidence = audit_finer(result, tape, m30)
        row, = evidence["rows"]
        self.assertEqual(evidence["same_coarse_bar_sl_confirmed_in_m30"], 1)
        self.assertEqual(evidence["same_exit_price"], 0)
        self.assertEqual(row["entry_delay_minutes_from_chart_stamp"], 60)
        self.assertEqual(row["exit_delay_minutes_from_chart_stamp"], 90)
        self.assertEqual(row["finer_candidate_exit_price"], 105.998)
        self.assertEqual(row["coarse_exit_price"], 108.0)
        self.assertEqual(result.trades[0].exit, 108.0)

    def test_gap_prioritizes_existing_protective_stop_in_conservative_model(self):
        tape = self.channel_tape(35)
        research = run(tape, SYMBOL, BASE)
        native = run(tape, SYMBOL, BASE, native=True)
        closed, = research.trades
        self.assertEqual(closed.reason, "SL_GAP")
        self.assertEqual(native.trades[0].reason, "CHANNEL")
        self.assertEqual(closed.exit_ms, tape[112].time_ms)
        self.assertAlmostEqual(closed.exit, 35 - 0.175 - 0.002)
        self.assertLess(closed.net_r, -1.0)
        self.assertAlmostEqual(closed.net_usd, closed.gross_usd - closed.spread_usd - closed.slippage_usd
                               - closed.commission_usd - closed.financing_usd)

    def test_next_available_open_when_chart_has_gap_and_no_fake_bar(self):
        gap = HOUR_MS * 60
        tape = [*narrow_seed(), bar(100, 100, 111, 99, 110),
                bar(101, 111, 115, 110, 114, gap=gap)]
        replay = run(tape, SYMBOL, BASE)
        self.assertEqual(replay.gaps, ((tape[100].close_ms, tape[101].time_ms),))
        self.assertEqual(replay.open_trade.entry_ms, tape[101].time_ms)
        self.assertEqual(len(replay.equity), 2)
        self.assertAlmostEqual(replay.open_trade.entry, 111.177)

    def test_qty_round_down_minimum_and_gap_capital_check(self):
        tape = [*narrow_seed(), bar(100, 100, 111, 99, 110), bar(101, 112, 114, 110, 113)]
        rejected = run(tape, Instrument(.001, 100, 1), BASE)
        self.assertEqual(find_event(rejected, "D20_REJECT")[0]["reason"], "RISK_BELOW_MIN_QTY")
        self.assertFalse(find_event(rejected, "D20_FILL"))
        kept = run(tape, Instrument(.001, 0.3, 1), ADVERSE)
        signal, = find_event(kept, "D20_SIGNAL")
        self.assertAlmostEqual(signal["qty"] / .3, round(signal["qty"] / .3))
        self.assertLessEqual(signal["risk_usd"], signal["risk_budget"])

        expensive = [bar(i, 99999, 99999.1, 99998.9, 99999) for i in range(100)]
        expensive += [bar(100, 99999, 99999.2, 99998.9, 99999.2),
                      bar(101, 101000, 101001, 100999, 101000)]
        research = run(expensive, Instrument(.001, .001, 1), BASE)
        self.assertTrue(find_event(research, "D20_SIGNAL")[0]["margin_capped"])
        failure, = find_event(research, "D20_CAPITAL_REJECT_AT_OPEN")
        self.assertEqual(failure["reason"], "CAPITAL_INSUFFICIENT_AT_FILL")
        self.assertFalse(find_event(research, "D20_FILL"))

    def test_same_start_prefix_causality_and_cutoff_pending(self):
        tape = self.channel_tape(105)
        whole = run(tape, SYMBOL)
        for n in range(100, len(tape)):
            with self.subTest(n=n):
                prefix = run(tape[:n], SYMBOL)
                through = tape[n - 1].close_ms
                known = [e for e in whole.events
                         if (e.data.get("seen_ms", e.data.get("time_ms", 0)) <= through
                             and e.data.get("bar", n - 1) < n)]
                self.assertEqual(list(prefix.events), known)
                self.assertEqual(list(prefix.equity), list(whole.equity[:len(prefix.equity)]))
        cutoff = tape[100].close_ms + 1
        clipped = run(tape, SYMBOL, cutoff_ms=cutoff)
        self.assertIsNotNone(clipped.pending)
        self.assertIsNone(clipped.open_trade)
        self.assertEqual(clipped.last_score_ms, tape[100].close_ms)
        self.assertEqual(verify_prefixes(tape, SYMBOL), {"status": "MATCH", "tested_prefixes": 4})

    def test_rollover_dst_and_funding_charged_separately(self):
        for first, last, date in (
            ("2024-03-06T21:59:59Z", "2024-03-06T22:00:00Z", "2024-03-06T22:00:00Z"),
            ("2024-03-13T20:59:59Z", "2024-03-13T21:00:00Z", "2024-03-13T21:00:00Z"),
        ):
            self.assertEqual(rollovers(utc_ms(first), utc_ms(last)), ((utc_ms(date), 3),))
        self.assertEqual(rollovers(utc_ms("2024-03-15T22:00:00Z"), utc_ms("2024-03-18T20:00:00Z")), ())
        tape = self.channel_tape(105)
        result = run(tape, SYMBOL, BASE)
        fees = find_event(result, "D20_FINANCING")
        self.assertGreater(len(fees), 0)
        trade, = result.trades
        self.assertAlmostEqual(sum(e["usd"] for e in fees), trade.financing_usd)
        self.assertAlmostEqual(result.equity[-1].equity_usd - INITIAL_CAPITAL, trade.net_usd)
        summary = summarize(result)
        self.assertEqual(summary["full"]["closed_trades"], 1)
        self.assertEqual(summary["calendar_years_utc"]["2024"]["closed_trades"], 1)
        self.assertAlmostEqual(summary["full"]["charged_financing_usd"], trade.financing_usd)
        self.assertEqual(summary["monthly_uncertainty"]["status"], "INSUFFICIENT_COMPLETE_MONTHS")

    def test_reject_overlapping_tape(self):
        with self.assertRaisesRegex(ValueError, "Duplicate, overlapping"):
            run([bar(0), bar(0)], SYMBOL)


if __name__ == "__main__":
    unittest.main()
