import copy
import unittest
from unittest.mock import patch

from research.backtesting import engine
from research.strategies.ablation import AblatedCandidateLibrary,AblationSpec,gate_ablation
from research.strategies.library import CandidateLibrary
from research.tests.test_engine_candidates import fixture


class AblationTests(unittest.TestCase):
    def test_only_registered_pairs_permitted(self):
        for family,gate in [('S1','CONFIRMATION_CANDLE'),('S3','STRUCTURAL_STOP_RANGE'),('S8','EXHAUSTION_DISTANCE')]:
            with self.assertRaises(ValueError):AblationSpec(family,gate)

    def test_only_named_gate_removed_other_rejections_preserved(self):
        original={'family':'S3','raw_setup':True,'status':'REJECTED','reasons':['CONFIRMATION_CANDLE','STRUCTURAL_STOP_RANGE'],'initial_stop':99,'reference':100}
        lib=AblatedCandidateLibrary(family='S3',gate='CONFIRMATION_CANDLE')
        with patch.object(CandidateLibrary,'evaluate',return_value=copy.deepcopy(original)):
            result=lib.evaluate('S3',1,1,[],[])
        self.assertEqual(result['status'],'REJECTED')
        self.assertEqual(result['reasons'],['STRUCTURAL_STOP_RANGE'])
        self.assertEqual(result['ablation_original_reasons'],original['reasons'])
        self.assertEqual(result['initial_stop'],99)

    def test_no_raw_trigger_and_other_family_are_unchanged(self):
        lib=AblatedCandidateLibrary(family='S3',gate='CONFIRMATION_CANDLE')
        for family,raw in [('S1',True),('S3',False)]:
            original={'raw_setup':raw,'status':'REJECTED','reasons':['CONFIRMATION_CANDLE']}
            with patch.object(CandidateLibrary,'evaluate',return_value=copy.deepcopy(original)):
                self.assertEqual(lib.evaluate(family,1,1,[],[]),original)

    def test_real_state_consumption_is_identical_on_trigger(self):
        bars,features=fixture([(100,100.3,99.8,100),(100.7,101.3,100.5,101.2),(101.1,101.4,101,101.3),(101.45,101.6,101,101.5)])
        baseline=CandidateLibrary();ablation=AblatedCandidateLibrary(family='S3',gate='CONFIRMATION_CANDLE')
        for i in (1,2):
            self.assertEqual(baseline.evaluate('S3',1,i,bars,features),ablation.evaluate('S3',1,i,bars,features))
        rejected=baseline.evaluate('S3',1,3,bars,features)
        admitted=ablation.evaluate('S3',1,3,bars,features)
        self.assertEqual(rejected['reasons'],['CONFIRMATION_CANDLE'])
        self.assertEqual(admitted['status'],'CONFIRMED')
        self.assertEqual(baseline.states,ablation.states)
        self.assertNotIn(('S3',1),ablation.states)

    def test_factory_restored_after_exception_and_nested_call_refused(self):
        original=engine.CandidateLibrary
        with self.assertRaisesRegex(RuntimeError,'synthetic failure'):
            with gate_ablation('S8','PRIOR_PAUSE'):
                self.assertIsInstance(engine.CandidateLibrary(),AblatedCandidateLibrary)
                with self.assertRaises(RuntimeError):
                    with gate_ablation('S3','CONFIRMATION_CANDLE'):pass
                raise RuntimeError('synthetic failure')
        self.assertIs(engine.CandidateLibrary,original)
        with gate_ablation('S5','CONFIRMATION_CANDLE'):pass
        self.assertIs(engine.CandidateLibrary,original)

    def test_parallel_process_state_and_foreign_override_refused(self):
        with patch('research.strategies.ablation.threading.active_count',return_value=2):
            with self.assertRaises(RuntimeError):
                with gate_ablation('S3','CONFIRMATION_CANDLE'):pass
        with patch.object(engine,'CandidateLibrary',object()):
            with self.assertRaises(RuntimeError):
                with gate_ablation('S3','CONFIRMATION_CANDLE'):pass


if __name__=='__main__':unittest.main()
