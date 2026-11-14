"""Pure accounting fixture. No native or market evidence is represented here."""

from decimal import Decimal as D
import unittest

from research.net_campaign_evidence.h1_native import report


BASE = "base"
NEW = "new"
CARRY = "carry"
M15 = report.INTERVAL


def simulated_fixture(variant):
    """One carry at the left boundary, one closed plan, optional open at cutoff."""
    start, end = report.START, report.END
    clocks = (start - 3 * M15, start - 2 * M15, start - M15, start,
              start + M15, start + 2 * M15, start + 3 * M15, start + 4 * M15, end)
    bars = tuple({"index": i, "open_ms": t - M15, "close_ms": t, "open": D(100),
                  "high": D(102), "low": D(96), "close": D(98) if i in {3, 8} else D(100)}
                 for i, t in enumerate(clocks))
    rows, events = [], []
    totals = dict(entered=0, closed=0, capacity_skipped=0, gross=D(0), costs=D(0), net=D(0))

    def record(plan, kind, i, at_open=False, *, px=D(100), fraction=None, amounts=None):
        known = clocks[i]
        event = {
            "event_id": f"{plan}|RAW|{kind}|{known}", "event": kind, "book": "RAW", "plan_id": plan,
            "known_time": known, "effective_time": bars[i]["open_ms"] if at_open else known,
            "price": px, "side": 1, "reference": D(100), "initial_sl": D(96),
            "tp1": D(104), "tp2": D(108), "tp3": D(112), "roundtrip_cost_price": D(".354"),
            "quantity_fraction": fraction, "reason": kind,
        }
        if amounts:
            gross, costs, net = amounts
            event.update(gross_r=gross, cost_r=costs, net_r=net)
        events.append(event)
        if kind in ("SIM_ENTRY", "CLOSED", "CAPACITY"):
            totals[{"SIM_ENTRY": "entered", "CLOSED": "closed", "CAPACITY": "capacity_skipped"}[kind]] += 1
        if kind == "CLOSED":
            for metric, amount in zip(("gross", "costs", "net"), amounts):
                totals[metric] += amount

    def settle(plan, i, price):
        gross = (price - 100) / 4
        split = (gross, D(".0885"), gross - D(".0885"))
        record(plan, "SIM_SL", i, px=price, fraction=D(1), amounts=split)
        record(plan, "CLOSED", i, px=price, fraction=D(1), amounts=split)

    for i, clock in enumerate(clocks):
        phase, plan = 0, ""
        if i == 1:
            record(CARRY, "QUEUED", i)
            phase, plan = 1, CARRY
        if i == 2:
            record(CARRY, "SIM_ENTRY", i, at_open=True, fraction=D(1))
            phase, plan = 2, CARRY
        if i == 3:
            phase, plan = 2, CARRY
        if i == 4:
            settle(CARRY, i, D(96))
            if variant:
                record(NEW, "QUEUED", i)
                phase, plan = 1, NEW
            else:
                record(BASE, "QUEUED", i)
                phase, plan = 1, BASE
        if i == 5:
            plan = NEW if variant else BASE
            record(plan, "SIM_ENTRY", i, at_open=True, fraction=D(1))
            phase = 2
        if i == 6:
            if variant:
                record(BASE, "CAPACITY", i)
                phase, plan = 2, NEW
                record(NEW, "SIM_TP1", i, px=D(104), fraction=D(".33333333"),
                       amounts=(D(".33333333"), D(".0295"), D(".30383333")))
            else:
                settle(BASE, i, D(96))
        if i > 6 and variant:
            phase, plan = 2, NEW
        rows.append({
            "close_ms": clock, "phase": phase, "plan_id": plan,
            "entry": D(100) if phase == 2 else None, "risk": D(4) if phase == 2 else None,
            "remaining": D(".66666667") if phase == 2 and i >= 6 else D(1) if phase == 2 else None,
            **totals,
        })
    return {
        "manifest": {
            "scoring_start_ms": start, "fixed_end_close_ms": end, "frame_minutes": 15, "lane": "B" if variant else "A"
        },
        "manifest_sha256": "right" if variant else "left",
        "bars": bars, "books": {"RAW": rows}, "events": events,
    }


VALIDATED = {"status": "VALIDATED_MATCHED_AB", "left_manifest_sha256": "left", "right_manifest_sha256": "right"}


class ReportTests(unittest.TestCase):
    def test_fixed_fill_bookkeeping_carry_partial_and_displacement(self):
        data = report.report_metrics(simulated_fixture(False), simulated_fixture(True), VALIDATED)
        native_a = data["lanes"]["A"]["native_r"]
        self.assertEqual(native_a["closed_cohort"]["net_r"], D("-1.0885"))
        self.assertEqual(native_a["filled"], 1)
        self.assertEqual(native_a["carry_in_campaigns"][0]["plan_id"], CARRY)
        for mode in ("normal", "stress"):
            a, b = (data["lanes"][lane][mode] for lane in ("A", "B"))
            self.assertAlmostEqual(a["closed_cohort"]["net_usd"], -250)
            self.assertEqual(a["closed_cohort"]["closed"], 1)
            self.assertEqual(b["closed_cohort"]["closed"], 0)
            residual, = b["open_or_pending_at_cutoff"]
            self.assertEqual(residual["valuation_at_cutoff"]["remaining_fraction"], D(".66666667"))
            self.assertGreater(residual["valuation_at_cutoff"]["realized_parts_net"], D(0))
            self.assertLess(
                residual["valuation_at_cutoff"]["liquidation_net_including_realized_parts"],
                residual["valuation_at_cutoff"]["realized_parts_net"],
            )
            marks = b["marked_close_equity"]["series_rebased"]
            self.assertEqual(marks[0], (report.START, D(0)))
            self.assertEqual(marks[-1][0], report.END)
            diff = data["observed_plan_changes_by_scenario"][mode]
            self.assertEqual(diff["confirmed_capacity_displacement_in_B"], 1)
            self.assertEqual(diff["counts"], {"A_ONLY_OBSERVED_FILL": 1, "B_ONLY_OBSERVED_FILL": 1})

    def test_blocks_unvalidated_and_incomplete_raw_stream(self):
        a, b = simulated_fixture(False), simulated_fixture(True)
        with self.assertRaises(ValueError):
            report.report_metrics(a, b, {"status": "INVALID_COMPARISON"})
        with self.assertRaises(ValueError):
            report.report_metrics(a, b, {**VALIDATED, "left_manifest_sha256": "different"})
        b["books"]["RAW"].pop()
        with self.assertRaises(ValueError):
            report.report_metrics(a, b, VALIDATED)

    def test_same_side_sl_cash_equals_initial_budget(self):
        event = {"event": "SIM_SL", "price": D(104), "initial_sl": D(104), "side": -1, "plan_id": "short"}
        entry = {**event, "price": D(100)}
        for scenario in report.cost_scenarios().values():
            model = report.new_fill(entry, scenario)
            price = report.priced_part(event, model, D(1), scenario)
            self.assertAlmostEqual(price, D(-250))


if __name__ == "__main__":
    unittest.main()
