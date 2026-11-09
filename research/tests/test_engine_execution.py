import unittest
from research.backtesting.engine import BacktestConfig, run_backtest


def market(rows):
    return [dict(zip(('open','high','low','close'),row),time=1704067200000+i*900000,time_close=1704067200000+(i+1)*900000,volume=100) for i,row in enumerate(rows)]


def signal(index=0,side=1,family='S1',reference=100,stop=None,**extra):
    return {'family':family,'side':side,'index':index,'confirmation_time':1704067200000+(index+1)*900000,'reference':reference,'initial_stop':stop if stop is not None else 100-side,'status':'CONFIRMED','raw_setup':True,'reasons':[],**extra}


def run(rows,signals=None,**config):
    data=market(rows)
    provider=lambda i,b,f:[dict(s) for s in (signals if signals is not None else [signal()]) if s['index']==i]
    return run_backtest(data,BacktestConfig(max_entry_deviation_atr=100,**config),candidate_provider=provider)


class ExecutionTests(unittest.TestCase):
    def test_disabled_default_is_no_trade(self):
        result=run_backtest(market([(100,101,99,100)]*4))
        self.assertEqual(result['plans'],[])
        self.assertEqual(result['config']['enabled_families'],())

    def test_buy_three_targets_fee_once_and_actual_r(self):
        r=run([(100,100,100,100),(100,103.5,99.5,103)])
        p=r['plans'][0]
        self.assertEqual(p['entry_index'],1)
        self.assertEqual(p['status'],'COMPLETE')
        self.assertEqual([x['price'] for x in p['exits']],[101,102,103])
        self.assertAlmostEqual(p['fees'],1.05)
        self.assertAlmostEqual(p['net_cash'],4.944)
        self.assertAlmostEqual(p['net_r'],4.944/3.006)
        self.assertEqual(p['partial_exit_count'],3)
        self.assertEqual(len({x['exit_component_id'] for x in p['exits']}),3)

    def test_sell_symmetry(self):
        buy=run([(100,100,100,100),(100,103.5,99.5,103)])['plans'][0]
        sell=run([(100,100,100,100),(100,100.5,96.5,97)],[signal(side=-1)])['plans'][0]
        self.assertAlmostEqual(buy['net_cash'],sell['net_cash'])
        self.assertAlmostEqual(buy['net_r'],sell['net_r'])

    def test_same_bar_ambiguous_all_stops_first(self):
        p=run([(100,100,100,100),(100,104,98,103)])['plans'][0]
        self.assertTrue(p['ambiguous'])
        self.assertEqual({x['reason'] for x in p['exits']},{'SL'})
        self.assertEqual(p['partial_exit_count'],3)
        self.assertAlmostEqual(p['net_cash'],-4.062)

    def test_stop_gap_preserves_loss_at_open_not_stop_price(self):
        p=run([(100,100,100,100),(98,98.5,97,98)])['plans'][0]
        self.assertTrue(p['gap_invalid'])
        self.assertEqual({x['reason'] for x in p['exits']},{'SL_GAP'})
        self.assertEqual({x['price'] for x in p['exits']},{97.998})
        self.assertIsNone(p['net_r'])
        self.assertLess(p['net_cash'],0)

    def test_open_limit_gap_is_before_later_intrabar_stop(self):
        p=run([(100,100,100,100),(102.5,103,98,99)])['plans'][0]
        self.assertEqual([x['reason'] for x in p['exits']],['TP1','TP2','SL'])
        self.assertEqual(p['exits'][0]['time_precision'],'BAR_OPEN')

    def test_partial_open_and_pending_not_whole_winners(self):
        r=run([(100,100,100,100),(100,101.2,99.5,100.5)])
        self.assertEqual(r['summary']['complete_plans'],0)
        self.assertEqual(r['summary']['open_plans'],1)
        self.assertEqual(r['plans'][0]['remaining_quantity'],2)
        self.assertAlmostEqual(r['plans'][0]['unallocated_entry_fee'],.35)
        p=run([(100,100,100,100)])
        self.assertEqual(p['summary']['pending_plans'],1)
        self.assertEqual(p['summary']['complete_plans'],0)

    def test_break_even_applies_next_bar_not_retroactively(self):
        p=run([(100,100,100,100),(100,101.2,99.5,100.5),(100.4,100.7,99.8,100)],exit_variant='BREAKEVEN')['plans'][0]
        self.assertEqual([r['index'] for r in p['exits']],[1,2,2])
        self.assertTrue(p['breakeven_active'])
        self.assertAlmostEqual(p['current_stop'],p['entry_price'])

    def test_time_exit_is_next_open(self):
        p=run([(100,100,100,100),(100,100.5,99.5,100.2),(100.7,101,100,100.8)],max_hold_bars=1)['plans'][0]
        self.assertEqual(p['final_exit_index'],2)
        self.assertEqual(p['exit_reason'],'TIME_LIMIT')
        self.assertAlmostEqual(p['exits'][0]['price'],100.698)

    def test_delay_is_actual_later_open(self):
        p=run([(100,100,100,100),(100,104,99.5,103),(100.2,100.5,99.5,100)],entry_delay_bars=2)['plans'][0]
        self.assertEqual(p['entry_index'],2)
        self.assertEqual(p['exits'],[])

    def test_opposing_signals_reject_both_without_arbitrary_side(self):
        r=run([(100,100,100,100)]*2,[signal(),signal(side=-1,family='S2')])
        self.assertFalse(r['plans'])
        self.assertEqual(r['summary']['rejection_reasons']['SIMULTANEOUS_OPPOSING_SIGNALS'],2)

    def test_priority_duplicate_and_position_occupancy(self):
        r=run([(100,100,100,100),(100,100.5,99.5,100),(100,100.5,99.5,100)],[signal(family='S2'),signal(),signal(),signal(index=1,family='S3')])
        self.assertEqual(len(r['plans']),1)
        self.assertEqual(r['plans'][0]['family'],'S1')
        for reason in ('FIXED_FAMILY_PRIORITY','DUPLICATE_SIGNAL','PORTFOLIO_OCCUPIED'):
            self.assertIn(reason,r['summary']['rejection_reasons'])

    def test_news_risk_and_daily_loss_fail_closed(self):
        for kwargs,reason in [({'manual_news_pause':True},'MANUAL_NEWS_PAUSE_NO_EXTERNAL_CALENDAR'),({'max_risk_cash':2},'MAX_PLANNED_RISK_CASH')]:
            r=run([(100,100,100,100)]*2,**kwargs)
            self.assertFalse(r['plans'])
            self.assertIn(reason,r['summary']['rejection_reasons'])
        r=run([(100,100,100,100),(100,100.5,98,99),(100,100,100,100)],[signal(),signal(index=1,family='S2',reference=99,stop=98)],daily_loss_limit_cash=1)
        self.assertEqual(len(r['plans']),1)
        self.assertIn('DAILY_REALIZED_LOSS_LIMIT',r['summary']['rejection_reasons'])

    def test_cash_cost_stress_does_not_double_apply_slippage(self):
        base=run([(100,100,100,100),(100,103.5,99.5,103)])['plans'][0]
        stressed=run([(100,100,100,100),(100,103.5,99.5,103)],commission_per_unit=.35)['plans'][0]
        self.assertAlmostEqual(base['net_cash']-stressed['net_cash'],1.05)
        self.assertEqual(base['entry_price'],stressed['entry_price'])

    def test_structural_target_unavailable_blocks_not_fabricated(self):
        r=run([(100,100,100,100)]*2,exit_variant='STRUCTURAL')
        self.assertFalse(r['plans'])
        self.assertIn('NO_STRUCTURAL_TARGET_AT_LEAST_1R',r['summary']['rejection_reasons'])

    def test_s6_s10_are_blocked_and_untraded(self):
        r=run_backtest(market([(100,101,99,100)]*4),BacktestConfig(enabled_families=('S6','S10')))
        self.assertEqual(set(r['blocked_families']),{'S6','S10'})
        self.assertFalse(r['plans'])

    def test_prefix_decisions_and_events_remain_unchanged(self):
        data=market([(100,100,100,100),(100,101.2,99.5,100.5),(100,104,99.5,103)])
        provider=lambda i,b,f:[signal()] if i==0 else []
        c=BacktestConfig(max_entry_deviation_atr=100)
        short=run_backtest(data[:2],c,candidate_provider=provider)
        full=run_backtest(data,c,candidate_provider=provider)
        self.assertEqual(short['decisions'],[d for d in full['decisions'] if d['index']<2])
        self.assertEqual(short['events'],[d for d in full['events'] if d['index']<2])


if __name__=='__main__':unittest.main()
