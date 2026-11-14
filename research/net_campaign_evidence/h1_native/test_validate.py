"""Fail-closed regressions for H1 native ingestion. Fixtures are synthetic."""

import csv
from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from research.net_campaign_evidence.h1_native import validate as h1
from research.net_campaign_evidence.h1_native.manifest import capture_manifest


STEP = 900_000
ORIGIN = h1.PRIMARY_FIRST_OPEN_MS
SETTINGS = {
    "core_source_commit": h1.BASELINE_COMMIT, "price_feed": "standard", "strategy_mode": "S5",
    "automatic_htf": True, "htf": "240", "manual_news_pause": False, "timed_news_window": False,
    "gap_filter": True, "evidence_shield": True,
}


def encode(value):
    return ("null" if value is None else str(value).lower() if type(value) is bool else str(value))


def line(tag, **kwargs):
    values = dict.fromkeys(h1.FIELDS[tag].split())
    values.update(version=1, lane="A", **kwargs)
    return "|".join([tag, *(encode(value) for value in values.values())])


def rows(first, after, *, terminal=False):
    common = {"symbol": "OANDA:XAUUSD", "frame": 15, "first_open_ms": ORIGIN}
    window = {
        **common, "requested_start_ms": ORIGIN + (first + 1) * STEP, "requested_end_ms": ORIGIN + after * STEP
    }
    output = [
        line("H1_META", **window, current_index=first, current_close_ms=window["requested_start_ms"],
             bar_count=0, event_count=0)
    ]
    for index in range(first, after):
        stamp = ORIGIN + (index + 1) * STEP
        output.append(line(
            "H1_BAR", symbol=common["symbol"], frame=15, index=index, open_ms=stamp-STEP, close_ms=stamp,
            open=100, high=102, low=98, close=101, prev_close=101 if index else None,
            gap=False, warmup=False, ready=False, session_pass=True, date_pass=True,
            news_paused=False, atr_pass=False, acceptable_bar=True,
        ))
        output.append(line(
            "H1_HTF", close_ms=stamp, core_frame=240, source_close1_ms=ORIGIN, close1=101
        ))
        for side in (1, -1):
            output.append(line(
                "H1_STATE", close_ms=stamp, side=side, before_phase=0, after_phase=0,
                raw=False, stock_cool="NA", exception_used=False, reason="No candidate",
                previous_accepted_plan_id="", qualified_cancel_plan_id="", global_paused=False,
                global_paused_at=0, family_paused=False, family_paused_at=0,
            ))
        for book in h1.BOOK_NAMES:
            initial_values = dict.fromkeys((*h1.COUNT_FIELDS, *h1.BOOK_AMOUNTS), 0)
            output.append(line(
                "H1_BOOK", close_ms=stamp, book=book, phase=0, plan_id="", remaining=1,
                hit1=False, hit2=False, hit3=False, exit_next_open=False, day_key=20260831,
                day_net=0, day_streak=0, **initial_values,
            ))
    if terminal:
        output.append(line("H1_LAST", **common, last_index=after-1, last_close_ms=window["requested_end_ms"]))
    output.append(line("H1_END", **window, current_index=after-1, current_close_ms=window["requested_end_ms"],
                       bar_count=after-first, event_count=0))
    return output


def write_csv(path, payload):
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("Date", "Message"))
        writer.writerows(("Operator display clock ignored", item) for item in payload)


def read_fixture(directory, ranges):
    windows = []
    for index, (first, after) in enumerate(ranges):
        path = directory / f"{index}.csv"
        write_csv(path, rows(first, after, terminal=index == len(ranges)-1))
        witness = h1.inspect_window(path)
        windows.append({"path": path.name, **{key: witness[key] for key in
                        ("sha256", "start_close_ms", "end_close_ms", "bars", "events")}})
    return windows


class ValidationTests(unittest.TestCase):
    def test_manifest_builder_writes_expected_settings_and_sorts_capture_files(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            paths = [root / "one.csv", root / "two.csv"]
            write_csv(paths[0], rows(0, 2))
            write_csv(paths[1], rows(2, 4, terminal=True))
            script = root / "diag.pine"
            script.write_bytes(b"collector source fixture")
            manifest = capture_manifest(paths[::-1], root / "capture.json", script, 5000, "test", "A", "add")
            self.assertEqual(manifest["settings"], SETTINGS)
            self.assertEqual([item["path"] for item in manifest["windows"]], ["one.csv", "two.csv"])

    def capture(self, directory, windows, *, depth=5000):
        manifest = directory / "capture.json"
        manifest.write_text(json.dumps({
            "schema": h1.SCHEMA, "lane": "A", "symbol": "OANDA:XAUUSD", "frame_minutes": 15,
            "chart_type": "standard", "chart_timezone": "UTC", "source_sha256": "a" * 64,
            "settings": SETTINGS, "modeled_costs": h1.FIXED_COSTS,
            "provenance": {"profile": "test", "mode": "add", "capture_id": "fixture"},
            "first_open_ms": ORIGIN, "fixed_end_close_ms": ORIGIN + 4 * STEP,
            "scoring_start_ms": ORIGIN + 2 * STEP, "calculated_bars": depth, "windows": windows,
        }), encoding="utf8")
        return manifest

    def test_complete_small_fixture_stitches_equal_verified_prefix(self):
        with TemporaryDirectory() as directory, patch.object(h1, "PRIMARY_DEPTH", 4):
            root = Path(directory)
            src = self.capture(root, read_fixture(root, ((0, 2), (2, 4))), depth=4)
            run = h1.validate_export(src)
            self.assertEqual(run["status"], "VALIDATED_COMPLETE_NATIVE_CAPTURE")
            self.assertEqual(tuple(run["bars_by_index"]), (0, 1, 2, 3))
            self.assertEqual({k: len(v) for k, v in run["books"].items()}, dict.fromkeys(h1.BOOK_NAMES, 4))
            self.assertEqual(len(run["state"]), 8)

    def test_missing_window_and_hash_mutation_fail(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self.capture(root, read_fixture(root, ((0, 1), (2, 4))))
            with self.assertRaisesRegex(ValueError, "missing/overlapping chart indices"):
                h1.validate_export(manifest)
            (root / "0.csv").write_text("tampered", encoding="utf8")
            with self.assertRaisesRegex(ValueError, "SHA-256 changed"):
                h1.validate_export(manifest)

    def test_hash_pinned_partial_tape_is_not_complete_or_authoritative(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self.capture(root, read_fixture(root, ((0, 2), (2, 4))))
            with self.assertRaisesRegex(ValueError, "Observed natural history does not match recorded Calculated bars"):
                h1.validate_export(manifest)

    def test_absent_footer_or_false_count_rejected_before_stitching(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "input.csv"
            payload = rows(0, 1)
            write_csv(source, payload[:-1])
            with self.assertRaisesRegex(ValueError, "missing or duplicate"):
                h1.inspect_window(source)
            write_csv(source, payload[:-1] + [payload[-1][:-1] + "2"])
            with self.assertRaisesRegex(ValueError, "footer does not confirm"):
                h1.inspect_window(source)

    def test_known_at_htf_clock_after_chart_open_rejected(self):
        open_ms = ORIGIN
        close = open_ms + STEP
        candle = {"index": 500, "open_ms": open_ms, "ready": True, "warmup": True}
        ctx = {"close_ms": close, "core_frame": "240", "source_close1_ms": close,
               "source_close3_ms": open_ms - 3000, "close1": Decimal(100), "ema50_1": Decimal(99),
               "ema50_3": Decimal(98), "atr_mean": Decimal(2), "rsi": Decimal(55), "adx": Decimal(20)}
        with self.assertRaisesRegex(ValueError, "after bar open"):
            h1._context([ctx], {close: candle}, {"settings": SETTINGS, "frame_minutes": 15})

    def test_duplicate_book_row_and_missing_state_are_rejected(self):
        stamp = ORIGIN + STEP
        candle = {"index": 0, "close_ms": stamp}
        empty = {"phase": 0, "plan_id": "", "remaining": Decimal(1), "lock_index": None,
                 **dict.fromkeys((*h1.COUNT_FIELDS, *h1.BOOK_AMOUNTS), 0)}
        book_rows = [{"close_ms": stamp, "book": book, **empty} for book in h1.BOOK_NAMES]
        book_rows.append(book_rows[-1])
        with self.assertRaisesRegex(ValueError, "incomplete native per-bar book state"):
            h1._books(book_rows, {stamp: candle}, {})
        with self.assertRaisesRegex(ValueError, "two native formation states"):
            h1._states([], {stamp: candle}, {}, {"settings": SETTINGS})

    def test_b_only_exception_requires_new_formation_after_cancel_is_known(self):
        clocks = [ORIGIN + n * STEP for n in range(1, 7)]
        bars = {t: {"index": i, "open_ms": t - STEP} for i, t in enumerate(clocks)}
        prefix = [(0, None, None), (0, None, None), (1, clocks[1], None),
                  (0, None, 2), (0, None, 2), (1, clocks[4], 2)]
        suffix = prefix[1:] + [(0, None, 5)]
        events = {
            clocks[2]: [{"event": kind, "book": book, "side": 1, "plan_id": "original"}
                        for book, kind in (("CORE", "CONFIRMED"), ("RAW", "QUEUED"))],
            clocks[3]: [{"book": "RAW", "side": 1, "plan_id": "original", "event": "CANCELLED",
                         "effective_time": clocks[3] - STEP}],
            clocks[5]: [{"book": "CORE", "side": 1, "plan_id": "new", "event": "CONFIRMED"}],
        }

        def formation(label, state):
            phase, born, accepted = state
            return {f"{label}_phase": phase, f"{label}_born_ms": born,
                    f"{label}_anchor": Decimal(99) if phase else None,
                    f"{label}_level": Decimal(101) if phase else None,
                    f"{label}_accepted_index": accepted}

        states = []
        for i, t in enumerate(clocks):
            for side in (1, -1):
                raw = side == 1 and i in (2, 5)
                changed = side == 1 and i == 5
                states.append({
                    "close_ms": t, "side": side, "raw": raw, "stock_cool": not changed if raw else None,
                    "exception_used": changed, "candidate_seed_ms": prefix[i][1] if raw else None,
                    "reason": h1.ACCEPTED_REASON if raw else "No candidate",
                    "same_side_last_fill_index": None, "same_side_last_fill_ms": None,
                    "previous_accepted_plan_id": "original" if side == 1 and i > 2 else "",
                    "qualified_cancel_plan_id": "original" if side == 1 and i > 3 else "",
                    "cancellation_known_ms": clocks[3] if side == 1 and i > 3 else None,
                    "global_paused": False, "global_paused_at": 0, "family_paused": False, "family_paused_at": 0,
                    **formation("before", prefix[i] if side == 1 else (0, None, None)),
                    **formation("after", suffix[i] if side == 1 else (0, None, None)),
                })
        self.assertEqual(h1._states(states, bars, events, {"settings": SETTINGS, "lane": "B"}), states)
        with self.assertRaisesRegex(ValueError, "exception exceeds"):
            h1._states(states, bars, events, {"settings": SETTINGS, "lane": "A"})
        same_seed = {**states[-2], "candidate_seed_ms": clocks[3]}
        states[-2] = same_seed
        with self.assertRaisesRegex(ValueError, "gate cooldown or candidate clock differs"):
            h1._states(states, bars, events, {"settings": SETTINGS, "lane": "B"})

    def test_capacity_and_close_events_cannot_be_dropped_from_ledger(self):
        no_events = dict.fromkeys((*h1.COUNT_FIELDS, *h1.BOOK_AMOUNTS), 0)
        wrong = {**no_events, "capacity_skipped": 1}
        with self.assertRaisesRegex(ValueError, "capacity_skipped lacks"):
            h1._book_counters(wrong, no_events, [])


if __name__ == "__main__":
    unittest.main()

