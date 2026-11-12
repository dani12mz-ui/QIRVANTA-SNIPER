"""Regressions for the selector's evidence and approval boundaries."""

import json
from unittest import TestCase

from research.professional_signal_selector.native_qa import PROTOCOL, source_checks
from research.professional_signal_selector.observations import source_edge, triage


RULES = json.loads(PROTOCOL.read_bytes())["policies"]["shared"]


def diagnostic_offer():
    return {
        "state": "ENTRY CANDIDATE",
        "policy": "P1_BREAKOUT",
        "closed_bar": True,
        "confirmed_observation": True,
        "approval": "RESEARCH ONLY",
        "htf": {"status": "ALIGNED"},
        "context": {"slot": "FREE"},
        "edge": {"close_beyond_in_direction": True},
        "directional_body_prior_atr": 0.5,
        "expires_at_utc": "2026-11-11T13:45:00Z",
        "plan": {
            "opposing_structure": 4026.6,
            "net_structural_room_r": 0.7,
            "modeled_cost": {"friction_r": 0.08},
        },
    }


class SelectorTests(TestCase):
    def test_only_known_confirmed_research_can_get_priority(self):
        offer = diagnostic_offer()
        self.assertEqual(triage(offer, {-1}, RULES)[0], "RESEARCH PRIORITY")
        self.assertEqual(offer["approval"], "RESEARCH ONLY")
        for field, value in (
            ("closed_bar", False),
            ("htf", {"status": "DATA MISSING"}),
            ("context", {"slot": "UNKNOWN"}),
            ("edge", None),
            ("expires_at_utc", None),
            ("plan", None),
        ):
            with self.subTest(field=field):
                self.assertEqual(triage({**offer, field: value}, {-1}, RULES)[0], "NO DATA")

    def test_risk_rejection_overrides_conflict(self):
        offer = diagnostic_offer()
        self.assertEqual(triage(offer, {-1, 1}, RULES)[0], "CONFLICT")
        offer["source_gate_rejections"] = ["INSUFFICIENT_NET_ROOM"]
        self.assertEqual(triage(offer, {-1, 1}, RULES)[0], "REJECT / EXPIRED")

    def test_observation_does_not_override_s5_permission(self):
        offer = diagnostic_offer()
        offer.update(state="READY", policy=None, approval="S5 APPROVED")
        self.assertEqual(triage(offer, {-1}, RULES)[0], "OBSERVE")
        self.assertEqual(offer["approval"], "S5 APPROVED")

    def test_level_must_predate_the_decision(self):
        self.assertTrue(source_edge(4000, 1000, 2000, 3999, -1, "SOURCE")["close_beyond_in_direction"])
        self.assertIsNone(source_edge(4000, 2000, 2000, 3999, -1, "SOURCE"))
        with self.assertRaises(ValueError):
            source_edge(4000, 3000, 2000, 3999, -1, "SOURCE")

    def test_pine_patch_is_display_only_and_opt_in(self):
        self.assertEqual(source_checks()["status"], "MATCH")
        self.assertFalse(source_checks()["selector_default"])
