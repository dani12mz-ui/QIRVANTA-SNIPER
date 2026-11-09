import json
import unittest
from datetime import datetime, timezone
from research.validation.native_report import excursion_bounds, fixed_fill_stress, label_events, sessions
from research.validation.protocol import ROOT


class NativeTests(unittest.TestCase):
    def setUp(self):
        self.bars=[{'time':i*1000,'time_close':(i+1)*1000,'high':h,'low':l} for i,(h,l) in enumerate([(110,90),(103,97),(120,80)])]
        self.c={'actual_entry':100,'side':1,'entry_bar_index':0,'entry_time':0,'last_exit_time':2000,'submitted_quantity':3,'initial_risk_cash':15,'exit_rows':[{'exit_bar_index':2,'exit_price':101,'size':3}],'realized_net_cash':2,'realized_commission_cash':1}

    def test_endpoint_extremes_are_only_upper_bounds(self):
        b=excursion_bounds(self.c,self.bars)
        self.assertEqual(b['mfe_price_lower'],3)
        self.assertEqual(b['mfe_price_upper'],20)
        self.assertEqual(b['mae_price_lower'],3)
        self.assertEqual(b['mae_price_upper'],20)
        self.assertFalse(b['quantity_weighted_cash_excursion_available'])

    def test_same_bar_entry_exit_no_full_bar_lower_bound(self):
        c=dict(self.c,last_exit_time=0,exit_rows=[{'exit_bar_index':0,'exit_price':101,'size':3}])
        b=excursion_bounds(c,self.bars)
        self.assertEqual(b['mfe_price_lower'],1)
        self.assertEqual(b['mae_price_lower'],0)

    def test_misaligned_timestamps_refused(self):
        c=dict(self.c,entry_time=1)
        self.assertEqual(excursion_bounds(c,self.bars)['status'],'UNRESOLVED_TIMESTAMP_OR_INDEX_ALIGNMENT')

    def test_cost_stress_no_double_base_fee(self):
        self.assertEqual(fixed_fill_stress([self.c],1,0)['net_cash'],2)
        self.assertAlmostEqual(fixed_fill_stress([self.c],2,2)['net_cash'],.988)

    def test_real_native_complete_accounting_and_causal_feature_schema(self):
        audit=json.loads((ROOT/'diagnostics/accounting/B-ALL.audit.json').read_text('utf-8'))
        self.assertEqual(len(audit['campaigns']),72)
        self.assertAlmostEqual(sum(c['realized_net_cash'] for c in audit['campaigns']),-144.492)
        events=label_events(audit['campaigns'])
        altered=json.loads(json.dumps(audit['campaigns']))
        altered[0]['actual_entry']+=100
        altered[0]['realized_net_cash']=-999999
        changed=label_events(altered)
        self.assertEqual(events[0]['x'],changed[0]['x'])
        self.assertGreater(events[0]['end'],audit['campaigns'][0]['last_exit_time'])

    def test_sessions_use_dst_not_constant_utc(self):
        def ms(s): return int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp()*1000)
        summer=sessions(ms('2026-08-03T07:30:00'))
        winter=sessions(ms('2026-11-09T07:30:00'))
        if summer=='TIMEZONE_DATABASE_UNAVAILABLE':
            self.skipTest('explicitly blocked without timezone database')
        self.assertIn('LONDON',summer.split('+'))
        self.assertNotIn('LONDON',winter.split('+'))


if __name__=='__main__': unittest.main()
