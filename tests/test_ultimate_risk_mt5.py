"""Synthetic fixtures only. No SDK or broker connection is needed for this suite."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from qirvanta_ultimate.feed_compare import compare_closed_bars
from qirvanta_ultimate.intents import IntentError, IntentLedger
from qirvanta_ultimate.mt5 import Mode, MT5Unavailable, ReviewedTerms, connect_readonly, linear_cfd_spec
from qirvanta_ultimate.risk import (BrokerSpec, ManagementPolicy, Quote, RiskPolicy, RiskState,
                                   TradePlan, assess_plan, manage_quote)
from qirvanta_ultimate.risk_stress import drawdown_duration, joint_adverse_loss, order_stress


class Fixture(unittest.TestCase):
    def setUp(self):
        self.plan = TradePlan('synthetic-plan', 'TEST_XAU', 1, 2000.2, 1998.,
                              (2003., 2004., 2006.), 1000, 30000)
        self.quote = Quote('TEST_XAU', 2000., 2000.2, 1050, 'TEST_SERVER')
        self.spec = BrokerSpec('TEST_XAU', 'TEST_SERVER', 'USD', True, 'SYNTHETIC_ONLY', 1000,
                               100., .01, .01, 1., .01, 100., .01, 20, 10, 7., True)
        self.state = RiskState(10000., 250., 'USD', 0., 0., 0., 0)
        self.policy = RiskPolicy(max_spread=.2, max_entry_deviation=.3)
        self.inputs = {'now_ms': 1100, 'margin_for_lots': lambda lots: lots*1000.,
                       'tick_value_profit_account_ccy': 1., 'session_open': True}

    def assess(self, *, plan=None, quote=None, spec=None, state=None, policy=None, **updates):
        return assess_plan(plan or self.plan, quote or self.quote, spec or self.spec,
                           state or self.state, policy or self.policy, **(self.inputs | updates))


class RiskGuardianTests(Fixture):
    def test_margin_rounds_down_on_grid_and_keeps_three_closable_parts(self):
        result = self.assess()
        self.assertTrue(result.allowed, result.reason)
        self.assertEqual(result.lots, .12)
        self.assertEqual(result.target_lots, (.04, .04, .04))
        self.assertAlmostEqual(result.estimated_loss_cash, .12*231.)
        self.assertEqual(result.estimated_margin_cash, 120.)
        self.assertGreater(result.net_reward_r, 1)
        self.assertFalse(result.orders_enabled)

    def test_weekly_limit_counts_reserved_risk_and_available_size(self):
        result = self.assess(state=replace(self.state, week_loss_cash=395., open_risk_cash=5.))
        self.assertEqual(result.reason, 'WEEKLY_LOSS_LIMIT')
        self.assertEqual(self.assess(policy=replace(self.policy, max_loss_cash=6.)).reason,
                         'RISK_BELOW_MINIMUM_THREE_PARTIALS')

    def test_quote_age_spread_and_exact_source_fail_closed(self):
        for quote, reason in (
            (replace(self.quote, at_ms=1101), 'STALE_OR_FUTURE_BID_ASK'),
            (replace(self.quote, ask=2000.201), 'SPREAD_LIMIT'),
            (replace(self.quote, server=None), 'UNVERIFIED_QUOTE_SERVER'),
            (replace(self.quote, symbol='TEST_BTC'), 'SYMBOL_MISMATCH'),
        ):
            with self.subTest(reason=reason):
                self.assertEqual(self.assess(quote=quote).reason, reason)

    def test_short_uses_bid_and_checks_ask_side_of_stop(self):
        short = replace(self.plan, side=-1, entry=2000., stop=2002., targets=(1997., 1996., 1994.))
        result = self.assess(plan=short)
        self.assertTrue(result.allowed, result.reason)
        self.assertEqual(result.execution_reference, self.quote.bid)
        self.assertEqual(self.assess(plan=replace(short, stop=2000.3)).reason,
                         'STOP_SIDE_OR_BROKER_DISTANCE')

    def test_missing_capabilities_and_hard_veto_precede_margin(self):
        def unexpectedly_called(_):
            self.fail('margin read during veto')
        self.assertEqual(self.assess(session_open=None, margin_for_lots=unexpectedly_called).reason,
                         'SESSION_UNVERIFIED')
        self.assertEqual(self.assess(news_blocked=True, margin_for_lots=unexpectedly_called).reason,
                         'NEWS_RISK_BLOCKED')
        self.assertEqual(self.assess(state=replace(self.state, cooldown_until_ms=1101)).reason,
                         'RISK_COOLDOWN')
        self.assertEqual(self.assess(tick_value_profit_account_ccy=None).reason,
                         'PROFIT_TICK_VALUE_UNVERIFIED')
        self.assertEqual(self.assess(margin_for_lots=lambda _: None).reason, 'MARGIN_UNVERIFIED')

    def test_bad_geometry_and_after_cost_reward(self):
        self.assertEqual(self.assess(plan=replace(self.plan, targets=None)).reason,
                         'INVALID_STOP_TARGET_OR_PARTIAL_GEOMETRY')
        self.assertEqual(self.assess(plan=replace(self.plan, targets=(2003.001, 2004., 2006.))).reason,
                         'TARGET_OFF_GRID_OR_NOT_EXECUTABLE')
        self.assertEqual(self.assess(tick_value_profit_account_ccy=.1).reason, 'NET_REWARD_RISK_LIMIT')

    def test_management_protective_fill_precedes_time_and_shock(self):
        params = {'fill_price': 2000.2, 'fill_at_ms': 1100, 'previous_quote_at_ms': 1150,
                  'current_stop': 1998., 'next_target': 0, 'tick_size': .01,
                  'policy': ManagementPolicy(100), 'now_ms': 1300, 'max_quote_age_ms': 50,
                  'expected_server': self.quote.server, 'exit_slippage_ticks': 2}
        gap = Quote(self.plan.symbol, 1995., 1997., 1300, self.quote.server)
        result = manage_quote(self.plan, gap, **params, volatility_shock=True)
        self.assertEqual(result.action, 'STOP')
        self.assertAlmostEqual(result.exit_price, 1994.98)
        healthy = replace(gap, bid=2001., ask=2001.2)
        self.assertEqual(manage_quote(self.plan, healthy, **params).action, 'TIME_STOP')
        self.assertEqual(manage_quote(self.plan, healthy, **(params | {'now_ms': 1400})).action,
                         'DATA_UNAVAILABLE')

    def test_target_progress_only_tightens_on_next_quote(self):
        params = {'fill_price': 2000.2, 'fill_at_ms': 1100, 'previous_quote_at_ms': 1150,
                  'current_stop': 1998., 'next_target': 0, 'tick_size': .01,
                  'policy': ManagementPolicy(1000, break_even_after_target=2),
                  'now_ms': 1300, 'max_quote_age_ms': 50, 'expected_server': self.quote.server}
        quote = replace(self.quote, bid=2003., ask=2003.2, at_ms=1300)
        first = manage_quote(self.plan, quote, **params)
        self.assertEqual(first.targets_hit, (0,))
        self.assertIsNone(first.next_stop)
        later = manage_quote(self.plan, replace(quote, bid=2004., ask=2004.2, at_ms=1310),
                             **(params | {'next_target': 1, 'now_ms': 1310, 'previous_quote_at_ms': 1300}))
        self.assertEqual(later.targets_hit, (1,))
        self.assertEqual(later.next_stop, 2000.2)

    def test_atr_trailing_never_loosens_and_requires_same_source(self):
        params = {'fill_price': 2000.2, 'fill_at_ms': 1100, 'previous_quote_at_ms': 1150,
                  'current_stop': 1998., 'next_target': 0, 'tick_size': .01,
                  'policy': ManagementPolicy(1000, atr_trail_multiplier=1.5),
                  'now_ms': 1300, 'max_quote_age_ms': 50, 'expected_server': self.quote.server}
        quote = replace(self.quote, bid=2002., ask=2002.2, at_ms=1300)
        tight = manage_quote(self.plan, quote, **params, atr=.7)
        self.assertEqual(tight.next_stop, 2000.95)
        loose = manage_quote(self.plan, quote, **(params | {'current_stop': 2000.95}), atr=1.2)
        self.assertIsNone(loose.next_stop)
        self.assertEqual(manage_quote(self.plan, replace(quote, server='OTHER'), **params).action,
                         'DATA_UNAVAILABLE')


class IntentLedgerTests(Fixture):
    def ledger(self, folder, mode=Mode.PAPER):
        return IntentLedger(Path(folder)/'paper.sqlite', portfolio='synthetic-book',
                            server='TEST_SERVER', account_currency='USD', mode=mode)

    def reserve(self, ledger, plan=None, **kwargs):
        return ledger.reserve(plan or self.plan, self.quote, self.spec, self.state, self.policy,
                              **(self.inputs | kwargs))

    def test_restart_reservation_and_event_reconciliation_are_idempotent(self):
        with TemporaryDirectory() as folder:
            ledger = self.ledger(folder)
            first = self.reserve(ledger)
            self.assertIsNotNone(first.intent)
            opened = ledger.reconcile(self.plan.plan_id, 'fill-1', 'PAPER_OPEN', at_ms=1110)
            ledger = self.ledger(folder)
            self.assertTrue(self.reserve(ledger).replayed)
            self.assertEqual(ledger.reconcile(self.plan.plan_id, 'fill-1', 'PAPER_OPEN', at_ms=1110), opened)
            with self.assertRaisesRegex(IntentError, 'CHANGED_PAYLOAD'):
                ledger.reconcile(self.plan.plan_id, 'fill-1', 'PAPER_CLOSED', at_ms=1110)
            with self.assertRaisesRegex(IntentError, 'CHANGED_PAYLOAD'):
                self.reserve(ledger, replace(self.plan, stop=1997.))
            ledger.reconcile(self.plan.plan_id, 'close-1', 'PAPER_CLOSED', at_ms=1120)
            self.assertEqual(len(ledger.audit()['events']), 3)

    def test_parallel_reservation_serializes_exposure_limit(self):
        with TemporaryDirectory() as folder, ThreadPoolExecutor(max_workers=2) as executor:
            ledger = self.ledger(folder)
            second = replace(self.plan, plan_id='other-synthetic-plan')
            results = list(executor.map(lambda p: self.reserve(ledger, p), (self.plan, second)))
            self.assertEqual(sum(r.intent is not None for r in results), 1)
            self.assertEqual(ledger.audit()['vetoes'][0]['reason'], 'POSITION_COUNT_LIMIT')

    def test_expiration_and_illegal_promotion(self):
        with TemporaryDirectory() as folder:
            ledger = self.ledger(folder, Mode.RESEARCH)
            self.reserve(ledger)
            with self.assertRaisesRegex(IntentError, 'PAPER_LEDGER_REQUIRED'):
                ledger.reconcile(self.plan.plan_id, 'fill', 'PAPER_OPEN', at_ms=1120)
            self.assertEqual(ledger.expire(self.plan.expires_at_ms), 1)
            self.assertEqual(ledger.expire(self.plan.expires_at_ms), 0)
            self.assertEqual(ledger.records()[0].status, 'EXPIRED')
        with TemporaryDirectory() as folder:
            for mode in (Mode.DEMO_EXECUTION, Mode.LIVE_EXECUTION):
                with self.assertRaisesRegex(MT5Unavailable, 'HARD_DISABLED'):
                    self.ledger(folder, mode)
            self.assertFalse((Path(folder)/'paper.sqlite').exists())


class FakeSDK:
    TIMEFRAME_M15 = 15
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1

    def __init__(self):
        self.calls = []
        self.account = SimpleNamespace(login=123456789, server='TEST_SERVER', company='TEST_BROKER',
                                       trade_mode=0, currency='USD', leverage=20, margin_mode=2,
                                       equity=10000., margin_free=5000.)
        self.symbol = SimpleNamespace(name='TEST_XAU', visible=True, select=True, custom=False,
                                      trade_mode=4, filling_mode=3, order_mode=49, trade_exemode=2,
                                      trade_calc_mode=2, chart_mode=0, trade_contract_size=100., point=.01,
                                      trade_tick_size=.01, trade_tick_value_loss=1., trade_tick_value_profit=1.,
                                      volume_min=.01, volume_max=100., volume_step=.01, volume_limit=5.,
                                      trade_stops_level=20, trade_freeze_level=10,
                                      currency_base='XAU', currency_profit='USD', currency_margin='USD')
        self.tick = SimpleNamespace(time_msc=1_799_999, bid=2000., ask=2000.2)

    def initialize(self, path, **kwargs):
        self.calls.append(('initialize', kwargs))
        return True

    def shutdown(self):
        self.calls.append(('shutdown',))

    def terminal_info(self):
        return SimpleNamespace(connected=True)

    def account_info(self):
        return self.account

    def symbols_get(self):
        return self.symbol, SimpleNamespace(name='TEST_BTC_UNIQUE')

    def symbol_info(self, symbol):
        return self.symbol if symbol == self.symbol.name else None

    def symbol_info_tick(self, symbol):
        return self.tick

    def order_calc_margin(self, order_type, symbol, volume, price):
        self.calls.append(('estimate', order_type, symbol, price))
        return volume*1000.

    def copy_rates_range(self, name, timeframe, start, end):
        self.calls.append(('history', timeframe, start.isoformat(), end.isoformat()))
        return tuple({'time': at, 'open': 100, 'high': 103, 'low': 98, 'close': 101,
                      'tick_volume': 5, 'real_volume': 0, 'spread': 20} for at in (0, 900, 1800))

    def positions_get(self, *, symbol):
        return (SimpleNamespace(symbol=symbol, type=0, volume=.2, ticket=987654321),)

    def orders_get(self, *, symbol):
        return (SimpleNamespace(symbol=symbol, type=5, volume_current=.3, ticket=999999999),)


class MT5ReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.terminal = Path(self.temp.name)/'terminal64.exe'
        self.terminal.write_bytes(b'SYNTHETIC_TEST_FIXTURE')
        self.sdk = FakeSDK()

    def connect(self, **updates):
        return connect_readonly(self.terminal, **({'expected_server': 'TEST_SERVER',
            'expected_company': 'TEST_BROKER', 'mode': Mode.DEMO_READ_ONLY,
            '_sdk': self.sdk, '_running_paths': [self.terminal], '_clock': lambda: 1_800_000} | updates))

    def test_explicit_attach_metadata_and_exact_closed_utc_bars(self):
        with self.connect() as reader:
            self.assertEqual(reader.discover_symbols('btc'), ('TEST_BTC_UNIQUE',))
            account = reader.account()
            self.assertEqual(account.kind, 'DEMO')
            self.assertNotIn('123456789', repr(account))
            self.assertIsNone(reader.symbol('TEST_XAU').session_open)
            quote = reader.quote('TEST_XAU')
            self.assertEqual(reader.margin(1, quote, .04), 40.)
            self.assertEqual(self.sdk.calls[-1], ('estimate', 0, 'TEST_XAU', 2000.2))
            self.assertEqual([b['at_ms'] for b in reader.bars('TEST_XAU', 15, 0, 1_800_000)], [0, 900_000])
            self.assertEqual(self.sdk.calls[-1][2], '1970-01-01T00:00:00+00:00')
        self.assertEqual(self.sdk.calls[0], ('initialize', {'timeout': 3000}))
        self.assertEqual(self.sdk.calls[-1], ('shutdown',))
        with self.assertRaisesRegex(MT5Unavailable, 'CONTEXT_CLOSED'):
            reader.account()

    def test_no_implicit_attach_or_account_change(self):
        for options in ({'_running_paths': []}, {'mode': Mode.LIVE_EXECUTION}, {'mode': Mode.RESEARCH}):
            with self.subTest(options=options), self.assertRaises(MT5Unavailable):
                with self.connect(**options):
                    self.fail('unexpected attach')
        self.assertEqual(self.sdk.calls, [])
        with self.assertRaisesRegex(MT5Unavailable, 'IDENTITY_OR_MODE_MISMATCH'):
            with self.connect(mode=Mode.LIVE_READ_ONLY):
                self.fail('demo metadata accepted for live-read mode')
        with self.connect() as reader:
            self.sdk.account.login += 1
            with self.assertRaisesRegex(MT5Unavailable, 'ACCOUNT_CHANGED'):
                reader.quote('TEST_XAU')

    def test_unselected_stale_and_missing_capabilities_fail_closed(self):
        with self.connect() as reader:
            self.sdk.symbol.select = False
            with self.assertRaisesRegex(MT5Unavailable, 'NOT_SELECTED'):
                reader.quote('TEST_XAU')
            self.sdk.symbol.select = True
            with self.assertRaisesRegex(MT5Unavailable, 'STALE'):
                reader.quote('TEST_XAU', max_age_ms=0)
            self.sdk.symbol.order_mode = 1
            with self.assertRaisesRegex(MT5Unavailable, 'CAPABILITY'):
                reader.margin(-1, reader.quote('TEST_XAU'), .1)
            self.assertFalse(any(c[0] == 'estimate' for c in self.sdk.calls))

    def test_fill_flags_and_redacted_exposure(self):
        with self.connect() as reader:
            self.assertEqual(reader.symbol('TEST_XAU').market_filling_options(), ('FOK', 'IOC'))
            snapshot = reader.exposure('TEST_XAU')
            self.assertEqual((snapshot.position_count, snapshot.pending_count), (1, 1))
            self.assertEqual((snapshot.long_volume, snapshot.short_volume), (.2, .3))
            self.assertNotIn('987654321', repr(snapshot))
            self.sdk.symbol.filling_mode = 4
            self.assertEqual(reader.symbol('TEST_XAU').market_filling_options(), ())
            with self.assertRaisesRegex(MT5Unavailable, 'CAPABILITY'):
                reader.margin(1, reader.quote('TEST_XAU'), .1)

    def test_vendor_symbol_requires_separate_verified_terms(self):
        with self.connect() as reader:
            symbol = reader.symbol('TEST_XAU')
        terms = ReviewedTerms(symbol.symbol, symbol.server, 'USD', symbol.calculation_mode,
                              1_800_000, 7., False, 'TEST_REVIEW_ONLY')
        with self.assertRaisesRegex(MT5Unavailable, 'UNVERIFIED'):
            linear_cfd_spec(symbol, terms)
        with self.assertRaisesRegex(MT5Unavailable, 'MISMATCH'):
            linear_cfd_spec(symbol, replace(terms, symbol='TEST_BTC_UNIQUE'))
        spec, profit_tick = linear_cfd_spec(symbol, replace(terms, linear_cfd_and_currency_verified=True))
        self.assertTrue(spec.verified)
        self.assertEqual(profit_tick, 1.)


class StressAndAlignmentTests(unittest.TestCase):
    def test_aligned_feeds_aggregate_gaps_and_refuse_duplicate_timestamps(self):
        a = [{'at_ms': at, 'close': value} for at, value in ((0, 100), (10, 101), (30, 99))]
        b = [{'at_ms': at, 'close': value} for at, value in ((0, 100), (10, 99), (20, 102))]
        result = compare_closed_bars(a, b, interval_ms=10)
        self.assertEqual((result['matched'], result['reference_only'], result['broker_only']), (2, 1, 1))
        self.assertEqual((result['reference_gap_count'], result['direction_disagreements']), (1, 1))
        self.assertEqual(result['mean_broker_minus_reference'], -1)
        with self.assertRaisesRegex(ValueError, 'NONMONOTONE'):
            compare_closed_bars(a+[a[-1]], b, interval_ms=10)

    def test_stress_counts_floor_crossings_and_duration(self):
        result = order_stress([-5., 1.], repetitions=11, seed=3, initial_cash=10., floor_cash=6.)
        self.assertEqual(result['floor_crossings'], 11)
        self.assertEqual(drawdown_duration([(0, 2.), (10, -1.), (20, 1.)]), 20)
        self.assertEqual(joint_adverse_loss(((50., 2., 4.), (80., 3., 9.))), 353.)


if __name__ == '__main__':
    unittest.main()
