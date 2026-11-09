import unittest
from tools.compare_baseline_reference import compare

class BaselineReferenceParity(unittest.TestCase):
    def test_same_supplied_candidates_costs_and_shadow_outcomes(self):
        report=compare()
        self.assertEqual(report['status'],'PASS_SYNTHETIC_REFERENCE_REPLAY')
        self.assertGreater(report['bars'],0)
        for run in report['runs']:
            self.assertGreater(run['supplied_candidates'],0)
            self.assertEqual(run['baseline_result_sha256'],run['unified_result_sha256'])
            self.assertGreater(sum(run['closed_counts'].values()),0)
            self.assertGreater(sum(run['event_counts'].values()),run['supplied_candidates'])

if __name__=='__main__':unittest.main()
