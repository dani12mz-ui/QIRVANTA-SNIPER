from dataclasses import replace
import json
import unittest

from gold_pro.evaluate import (
    Approval, Campaign, DAY_MS, ForwardEvidence, Lane, OffEventObservation,
    Opportunity, Study, WEEK_MS, evaluate, registered_protocol_sha256,
)


DATA = "a" * 64
RULES = "b" * 64
SOURCE = "c" * 64


def lane(identifier, returns, weeks=16):
    campaigns = []
    opportunities = []
    off_event = []
    for week in range(weeks):
        for day, cash in enumerate(returns):
            start = week * WEEK_MS + day * DAY_MS + 60_000
            campaigns.append(Campaign(
                id=f"{week}-{day}", entry_ms=start, exit_ms=start + 60_000,
                risk_cash=100, net_cash=cash, stressed_net_cash=cash - 1,
                mfe_cash=max(cash, 0) + 4, costs_cash=1, native_net_cash=cash / 2,
            ))
        for index, side in enumerate(("BUY", "SELL"), start=1):
            event_time = week * WEEK_MS + index * DAY_MS
            opportunities.append(Opportunity(
                f"{week}-{side}", event_time, side, True, True, True, 0.5, 0.2,
            ))
        off_event.append(OffEventObservation(week * WEEK_MS + 3 * DAY_MS, 1, 1))
    return Lane(
        identifier, tuple(campaigns), DATA, "COST", "RISK",
        axis="entry" if identifier != "S5" else "reference", source_sha256=SOURCE,
        opportunities=opportunities, off_event_observations=off_event,
    )


def study(forward=False):
    return Study(
        start_ms=0, end_ms=16 * WEEK_MS, registered_at_ms=7 * WEEK_MS,
        last_inspected_ms=7 * WEEK_MS, validation_start_ms=4 * WEEK_MS, fold_days=7,
        data_sha256=DATA, cost_model_id="COST", risk_policy_id="RISK",
        rules_sha256=RULES, registered_candidate_ids=("BETTER", "WIN_RATE"),
        min_campaigns=10, min_forward_weeks=8, bootstrap_repetitions=200,
        forward=(ForwardEvidence(8 * WEEK_MS, 16 * WEEK_MS, 17 * WEEK_MS, RULES, DATA,
                                 "d" * 64, "e" * 64) if forward else None),
    )


class PerformanceEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.reference = lane("S5", (-10, 18, -6, 10))
        self.better = lane("BETTER", (-4, 35, -3, 20))
        self.win_rate = lane("WIN_RATE", (5, 6, 7, -40))

    def registered_forward_study(self):
        registration = study(forward=True)
        forward = registration.forward
        protocol = registered_protocol_sha256(
            registration, self.reference, [self.better, self.win_rate], forward.start_ms, forward.end_ms,
        )
        return replace(registration, forward=replace(forward, protocol_sha256=protocol))

    def test_more_wins_can_have_worse_expectancy_and_extreme_loss(self):
        report = evaluate(self.reference, [self.better, self.win_rate], study())
        result = report["candidates"]["WIN_RATE"]
        self.assertEqual(result["tradeoffs"]["directions_vs_s5"]["win_rate"], "IMPROVED")
        self.assertEqual(result["tradeoffs"]["directions_vs_s5"]["expectancy_r"], "WORSENED")
        self.assertIn("TAIL_LOSS", result["reasons"])
        self.assertEqual(result["status"], "REJECTED")
        self.assertEqual(report["candidates"]["BETTER"]["status"], "INCONCLUSIVE")
        self.assertEqual(report["decision"], "RETAIN_IMMUTABLE_REFERENCE")
        self.assertEqual(report["evaluation"]["S5"]["native_net_cash_count"], 48)
        self.assertEqual(report["pareto_front_descriptive"]["non_dominated"], ["BETTER"])
        json.dumps(report, allow_nan=False)

    def test_initial_risk_and_matched_data_are_enforced(self):
        with self.assertRaisesRegex(ValueError, "different market data"):
            evaluate(self.reference, [replace(self.better, data_sha256="f" * 64)], study())
        high_risk = tuple(replace(c, risk_cash=251) for c in self.better.campaigns)
        report = evaluate(self.reference, [replace(self.better, campaigns=high_risk)], study())
        self.assertIn("COMPARABLE_RISK_BUDGET", report["candidates"]["BETTER"]["reasons"])
        duplicate = (*self.better.campaigns, self.better.campaigns[0])
        with self.assertRaisesRegex(ValueError, "campaign ID"):
            evaluate(self.reference, [replace(self.better, campaigns=duplicate)], study())

    def test_walk_forward_purges_straddling_trade_and_selects_using_training_only(self):
        base_study = study()
        cutoff = base_study.validation_start_ms
        boundary = Campaign("boundary", cutoff - 1, cutoff + 1, 100, -999, -1000)
        campaigns = (*self.better.campaigns, boundary)
        changed_after_cutoff = tuple(replace(c, net_cash=-900, stressed_net_cash=-901)
                                     if c.entry_ms >= cutoff else c for c in campaigns)
        baseline_report = evaluate(self.reference, [self.better], base_study)
        changed = evaluate(self.reference, [replace(self.better, campaigns=changed_after_cutoff)], base_study)
        before, after = (report["historical_walk_forward"][0] for report in (baseline_report, changed))
        self.assertEqual(before["selection"], "BETTER")
        self.assertEqual(before["selection"], after["selection"])
        self.assertEqual(before["development"]["BETTER"]["net_cash"], after["development"]["BETTER"]["net_cash"])
        self.assertEqual(after["development"]["BETTER"]["boundary_excluded_count"], 1)
        self.assertEqual(after["validation"]["BETTER"]["boundary_excluded_count"], 1)
        self.assertLess(after["validation"]["BETTER"]["net_cash"], before["validation"]["BETTER"]["net_cash"])

    def test_paired_forward_and_approval_are_bound_to_evidence_and_candidate_family(self):
        registered = self.registered_forward_study()
        lanes = [self.better, self.win_rate]
        report = evaluate(self.reference, lanes, registered)
        self.assertEqual(report["evaluation_scope"], "FORWARD_WITH_ATTESTED_PROVENANCE")
        candidate = report["candidates"]["BETTER"]
        self.assertEqual(candidate["status"], "AWAITING_EXPLICIT_APPROVAL")
        inference = candidate["paired_week_bootstrap"]
        self.assertEqual(inference["weeks"], 8)
        self.assertGreater(inference["ci95"][0], 0)
        self.assertGreater(inference["holm_p_one_sided"], inference["p_one_sided"])
        approved = Approval("BETTER", registered.study_id, report["evidence_sha256"], "research owner", 17 * WEEK_MS)
        final = evaluate(self.reference, lanes, registered, approved)
        self.assertEqual(final["approved_candidate"], "BETTER")
        self.assertEqual(final["decision"], "APPROVED_FOR_MANUAL_REVIEW")
        stale = replace(approved, evidence_sha256="0" * 64)
        self.assertEqual(evaluate(self.reference, lanes, registered, stale)["decision"], "RETAIN_IMMUTABLE_REFERENCE")
        subset = evaluate(self.reference, [self.better], registered)
        self.assertIn("CANDIDATE_FAMILY_NOT_FROZEN", subset["forward_failures"])
        unsealed = replace(registered, forward=replace(registered.forward, first_access_ms=9 * WEEK_MS))
        self.assertIn("FORWARD_CHRONOLOGY", evaluate(self.reference, lanes, unsealed)["forward_failures"])
        tuned = replace(registered, max_drawdown_ratio=1.5)
        self.assertIn("FROZEN_PROTOCOL_MISMATCH", evaluate(self.reference, lanes, tuned)["forward_failures"])

    def test_opportunity_gates_guard_both_directions(self):
        weakened = tuple(replace(row, actionable=False, filled=False, capture_fraction=None, net_r=None)
                         if row.side == "SELL" and row.time_ms >= 8 * WEEK_MS else row
                         for row in self.better.opportunities)
        result = evaluate(self.reference, [replace(self.better, opportunities=weakened), self.win_rate],
                          self.registered_forward_study())
        sell = result["opportunity_evaluation"]["BETTER"]["by_side"]["SELL"]
        self.assertEqual(sell["fill_recall"], 0)
        self.assertIn("SELL_ACTIONABLE_RECALL", result["candidates"]["BETTER"]["reasons"])
        self.assertIn("SELL_EVENT_CAPTURE", result["candidates"]["BETTER"]["reasons"])
        self.assertEqual(result["decision"], "RETAIN_IMMUTABLE_REFERENCE")
        missing = replace(self.better, opportunities=self.better.opportunities[1:])
        with self.assertRaisesRegex(ValueError, "same independently defined event calendar"):
            evaluate(self.reference, [missing], study())


if __name__ == "__main__":
    unittest.main()
