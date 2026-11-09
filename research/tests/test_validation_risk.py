import unittest
from dataclasses import replace
from research.risk.calculator import BrokerSpec, size_position


class RiskTests(unittest.TestCase):
    def setUp(self):
        # SYNTHETIC unit test profile; not a PU Prime account specification.
        self.spec=BrokerSpec('SYNTHETIC','TEST','USD',True,'UNIT_TEST_FIXTURE',1000,100,.01,.01,1,.01,100,.01,20,10,7,True)
        self.args=dict(side=1,bid=2000,ask=2000.2,quote_at_ms=1000,now_ms=1100,stop=1998,equity=10000)

    def test_unverified_never_sizes(self):
        r=size_position(replace(self.spec,verified=False),**self.args)
        self.assertEqual(r['status'],'BLOCKED')
        self.assertIsNone(r['lots'])

    def test_sizing_rounds_down_with_spread_fees_and_slip(self):
        r=size_position(self.spec,**self.args)
        self.assertEqual(r['lots'],.21)
        self.assertAlmostEqual(r['estimated_loss_per_lot'],231)
        self.assertLessEqual(r['estimated_loss_cash'],r['risk_budget_cash'])
        self.assertFalse(r['orders_enabled'])

    def test_minimum_lot_not_rounded_up(self):
        a=dict(self.args,equity=100,risk_fraction=.001)
        self.assertEqual(size_position(self.spec,**a)['reason'],'RISK_BELOW_MINIMUM_LOT')

    def test_stop_distance_uses_bid_for_long(self):
        a=dict(self.args,stop=1999.9)
        self.assertEqual(size_position(self.spec,**a)['reason'],'STOP_SIDE_OR_BROKER_DISTANCE')

    def test_short_uses_ask_for_stop(self):
        a=dict(self.args,side=-1,stop=2000.3)
        self.assertEqual(size_position(self.spec,**a)['reason'],'STOP_SIDE_OR_BROKER_DISTANCE')

    def test_stale_quote_and_spec(self):
        self.assertEqual(size_position(self.spec,**dict(self.args,now_ms=10000))['reason'],'STALE_OR_FUTURE_BID_ASK')
        self.assertEqual(size_position(self.spec,**dict(self.args,now_ms=90000000,quote_at_ms=90000000))['reason'],'STALE_OR_FUTURE_SPECIFICATION')

    def test_manual_pause_and_limits(self):
        self.assertEqual(size_position(self.spec,**dict(self.args,manual_news_pause=True))['reason'],'MANUAL_NEWS_PAUSE')
        self.assertEqual(size_position(self.spec,**dict(self.args,realized_daily_loss=200))['reason'],'DAILY_LOSS_LIMIT')
        self.assertEqual(size_position(self.spec,**dict(self.args,existing_risk_cash=100))['reason'],'PORTFOLIO_RISK_LIMIT')

    def test_currency_conversion_and_tick_grid(self):
        self.assertEqual(size_position(replace(self.spec,currency_conversion_verified=False),**self.args)['status'],'BLOCKED')
        self.assertEqual(size_position(self.spec,**dict(self.args,stop=1998.005))['reason'],'STOP_OFF_TICK_GRID')

    def test_volume_step_is_zero_anchored_not_minimum_anchored(self):
        spec=replace(self.spec,volume_min=.03,volume_step=.02)
        result=size_position(spec,**self.args)
        self.assertEqual(result['lots'],.2)
        too_small=size_position(spec,**dict(self.args,equity=1500))
        self.assertEqual(too_small['reason'],'RISK_BELOW_MINIMUM_LOT_ON_STEP_GRID')

    def test_missing_or_nonfinite_verified_parameters_still_block(self):
        for field,value in [('tick_value_loss_account_ccy',None),('volume_step',float('nan')),('contract_size',True)]:
            self.assertEqual(size_position(replace(self.spec,**{field:value}),**self.args)['status'],'BLOCKED')
        self.assertEqual(size_position(self.spec,**dict(self.args,quote_at_ms=None))['reason'],'INVALID_TIMESTAMP_OR_AGE_LIMIT')


if __name__=='__main__': unittest.main()
