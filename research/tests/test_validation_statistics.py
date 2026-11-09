import unittest

from research.validation.statistics import (block_bootstrap, cash_metrics,
    chronological_split, holm, sign_flip_p, walk_forward, wilson)


class StatisticalTests(unittest.TestCase):
    def test_wilson_never_certifies_one_win(self):
        low,high=wilson(1,1)
        self.assertLess(low,.3)
        self.assertAlmostEqual(high,1)

    def test_wilson_zero_sample_is_undefined(self):
        self.assertEqual(wilson(0,0),[None,None])

    def test_all_losses_and_zero_trades_remain(self):
        m=cash_metrics([-1,-3,-2])
        self.assertEqual(m['n'],3)
        self.assertEqual(m['wins'],0)
        self.assertEqual(m['profit_factor'],0)
        self.assertEqual(m['campaign_close_drawdown'],6)
        self.assertEqual(cash_metrics([])['profit_factor_status'],'NO_TRADES')

    def test_holm_uses_all_trials(self):
        self.assertEqual(holm([.01,.03,1]),[.03,.06,1])
        self.assertGreater(holm([.01]+[1]*79)[0],.05)

    def test_bootstrap_seed_reproducible_and_interval_not_total(self):
        a=block_bootstrap([-2,-1,1,2],repetitions=100,block=2,seed=2)
        self.assertEqual(a,block_bootstrap([-2,-1,1,2],repetitions=100,block=2,seed=2))
        self.assertEqual(a['mean'],0)
        self.assertLessEqual(a['ci95'][0],0)
        self.assertGreaterEqual(a['ci95'][1],0)

    def test_whole_sample_block_never_claims_zero_uncertainty(self):
        result=block_bootstrap([1,2,3,4,5],block=5)
        self.assertEqual(result['ci95'],[None,None])
        self.assertEqual(result['status'],'INSUFFICIENT_BLOCKS')
        self.assertEqual(result['mean'],3)

    def test_sign_flip_keeps_all_losses_as_failure(self):
        self.assertEqual(sign_flip_p([-1,-2,-3],repetitions=100),1)
        self.assertEqual(sign_flip_p([]),1)

    def test_purge_overlap_and_embargo(self):
        events=[{'id':i,'start':i*100,'end':i*100+20} for i in range(10)]
        events[5]['end']=650
        events[7]['end']=800
        s=chronological_split(events,embargo_ms=10)
        self.assertEqual([e['id'] for e in s['purged']],[5,7])
        self.assertTrue(all(e['end']<s['validation'][0]['start']-10 for e in s['train']))
        self.assertTrue(all(e['end']<s['test'][0]['start']-10 for e in s['validation']))

    def test_walkforward_never_uses_future_label(self):
        events=[{'id':i,'start':i*100,'end':i*100+150} for i in range(12)]
        for fold in walk_forward(events,initial_train=4,test_size=3,embargo_ms=20):
            self.assertTrue(all(e['end']<fold['test'][0]['start']-20 for e in fold['train']))


if __name__=='__main__': unittest.main()
