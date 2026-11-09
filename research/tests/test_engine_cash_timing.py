import csv
from pathlib import Path
import tempfile
import unittest

from research.backtesting.engine import BacktestConfig,run_backtest
from research.data.loader import load_bars
from research.tests.test_engine_execution import market,signal,run


class PaidCashTests(unittest.TestCase):
    def test_open_position_entry_fee_is_cash_and_drawdown_immediately(self):
        r=run([(100,100,100,100),(100,100.5,99.5,100)])
        p=r['plans'][0]
        self.assertEqual(p['net_cash'],0)
        self.assertAlmostEqual(p['cash_paid_net'],-.525)
        self.assertAlmostEqual(p['fees_paid'],.525)
        self.assertAlmostEqual(r['summary']['all_realized_net_cash'],-.525)
        self.assertAlmostEqual(r['summary']['cash_paid_drawdown'],.525)
        self.assertEqual(r['summary']['closed_parts_net'],0)
        self.assertEqual(r['summary']['closed_cash_drawdown'],0)
        self.assertEqual(r['equity_cash_paid'][0]['kind'],'ENTRY_FEE')

    def test_partial_open_paid_cash_is_closed_net_minus_unallocated_entry_fee(self):
        r=run([(100,100,100,100),(100,101.2,99.5,100.5)])
        p=r['plans'][0]
        self.assertAlmostEqual(p['cash_paid_net'],p['net_cash']-p['unallocated_entry_fee'])
        self.assertAlmostEqual(r['summary']['closed_parts_net']-r['summary']['cash_paid_net'],.35)
        self.assertAlmostEqual(p['fees_paid'],.7)

    def test_complete_cash_and_fees_reconcile_once(self):
        r=run([(100,100,100,100),(100,103.5,99.5,103)])
        p=r['plans'][0]
        self.assertAlmostEqual(p['cash_paid_net'],p['net_cash'])
        self.assertAlmostEqual(p['fees_paid'],p['fees'])
        self.assertAlmostEqual(sum(e['cash_change'] for e in r['equity_cash_paid']),p['net_cash'])
        self.assertAlmostEqual(r['summary']['cash_paid_drawdown'],.525)
        self.assertAlmostEqual(sum(e['allocated_entry_fee'] for e in p['exits']),p['entry_fee_paid'])

    def test_cross_day_guard_assigns_entry_fee_to_entry_day_only(self):
        bars=market([(100,100,100,100),(100,100.5,99.5,100),(100,100.5,99.5,100)])
        shift=94*900000  # signal 23:30, entry 23:45, scheduled exit next UTC midnight
        for b in bars:
            b['time']+=shift;b['time_close']+=shift
        def provider(i,b,f):
            if i not in (0,2):return []
            d=signal(index=i,family='S1' if i==0 else 'S2')
            d['confirmation_time']=bars[i]['time_close']
            return [d]
        r=run_backtest(bars,BacktestConfig(max_entry_deviation_atr=100,max_hold_bars=1,daily_loss_limit_cash=.8),candidate_provider=provider)
        self.assertEqual(len(r['plans']),2)
        self.assertEqual(r['plans'][1]['status'],'PENDING')
        days=list(r['summary']['daily_cash_UTC'].values())
        self.assertEqual(len(days),2)
        self.assertAlmostEqual(days[0],-.525)
        self.assertAlmostEqual(days[1],-.537)
        self.assertEqual(r['plans'][0]['exit_reason'],'TIME_LIMIT')

    def test_same_day_guard_counts_paid_entry_fee(self):
        r=run([(100,100,100,100),(100,100.5,99.5,100)],[signal(),signal(index=1,family='S2')],daily_loss_limit_cash=.4)
        self.assertIn('DAILY_REALIZED_LOSS_LIMIT',r['decisions'][-1]['reasons'])

    def test_fractional_and_nonfinite_csv_timestamps_are_rejected_before_cast(self):
        for value in ('1704067200000.5','1704067200000.00001','nan','inf'):
            with self.subTest(value=value),tempfile.TemporaryDirectory() as folder:
                p=Path(folder)/'bad.csv'
                p.write_text('time,time_close,open,high,low,close,volume\n'+f'{value},1704068100000,100,101,99,100,1\n',encoding='utf-8')
                with self.assertRaises(ValueError):load_bars(p)


if __name__=='__main__':unittest.main()
