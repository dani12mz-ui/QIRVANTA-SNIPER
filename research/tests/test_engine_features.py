import math
import unittest
from datetime import datetime, timezone

from research.data.loader import validate_bars
from research.features.causal import build_features, ClosedContext


def bars(n=260,start=1704067200000,minutes=15):
    result=[]
    for i in range(n):
        c=100+.03*i+math.sin(i/4)
        result.append({'time':start+i*minutes*60000,'time_close':start+(i+1)*minutes*60000,'open':c-.1,'high':c+.6,'low':c-.7,'close':c,'volume':100})
    return result


class FeatureTests(unittest.TestCase):
    def test_prefix_features_never_change_when_future_is_appended(self):
        data=bars(360)
        full=build_features(data)
        for n in (7,57,201,259,333):
            self.assertEqual(build_features(data[:n]),full[:n])

    def test_confirmed_pivot_is_released_after_three_right_bars(self):
        data=bars(10)
        for i,b in enumerate(data): b.update(open=100,close=100,low=99,high=101)
        data[3]['high']=110
        f=build_features(data)
        self.assertTrue(all(x['pivot_high'] is None for x in f[:6]))
        self.assertEqual(f[6]['pivot_high'],110)
        self.assertEqual(f[6]['pivot_high_confirmed_index'],6)

    def test_missing_data_fails_closed_not_imputed(self):
        for key,value in (('close',None),('low',float('nan')),('high',99),('time',float('inf'))):
            data=bars(2);data[1][key]=value
            with self.assertRaises(ValueError): build_features(data)

    def test_duplicate_and_mixed_timeframes_rejected(self):
        data=bars(3);data[1]=dict(data[0])
        with self.assertRaises(ValueError):validate_bars(data)
        data=bars(3);data[2]['time_close']+=300000
        with self.assertRaises(ValueError):validate_bars(data)

    def test_m5_m15_m30_supported_without_manufacturing_data(self):
        for minutes in (5,15,30):
            self.assertEqual(len(build_features(bars(220,minutes=minutes))),220)

    def test_completed_htf_release_never_before_close(self):
        data=bars(4);ctx=ClosedContext(60)
        self.assertIsNone(ctx.add(data[0])['available_at'])
        self.assertIsNone(ctx.add(data[1])['available_at'])
        self.assertIsNone(ctx.add(data[2])['available_at'])
        self.assertEqual(ctx.add(data[3])['available_at'],data[3]['time_close'])

    def test_incomplete_htf_bucket_does_not_fabricate_context(self):
        data=bars(4);ctx=ClosedContext(60)
        for b in data[1:]:last=ctx.add(b)
        self.assertIsNone(last['close'])
        self.assertIsNone(last['bias'])

    def test_tick_volume_does_not_enable_true_vwap(self):
        data=bars(260)
        for b in data:b['bar_vwap']=b['close']
        self.assertFalse(any(f['vwap_available'] for f in build_features(data)))
        self.assertFalse(any(f['vwap_available'] for f in build_features(data,volume_kind='TICK',verified_bar_vwap=True)))
        self.assertTrue(build_features(data,volume_kind='EXCHANGE_TRADED',verified_bar_vwap=True)[0]['vwap_available'])

    def test_invalid_volume_invalidates_whole_session(self):
        data=bars(6)
        for b in data:b['bar_vwap']=b['close']
        data[2]['volume']=0
        f=build_features(data,volume_kind='EXCHANGE_TRADED',verified_bar_vwap=True)
        self.assertTrue(f[1]['vwap_available'])
        self.assertTrue(all(not x['vwap_available'] for x in f[2:]))

    def test_london_and_ny_dst_use_different_transition_weeks(self):
        for day,expected in [('2026-03-16T00:00:00+00:00',(8,13)),('2026-04-06T00:00:00+00:00',(7,13)),('2026-11-02T00:00:00+00:00',(8,14))]:
            start=int(datetime.fromisoformat(day).timestamp()*1000)
            f=build_features(bars(96,start=start))
            if not f[0]['session_timezone_available']:
                self.skipTest('IANA database unavailable; engine explicitly blocks S7')
            london=next(x for x in f if 'LONDON' in x['opening_ranges'])
            ny=next(x for x in f if 'NEW_YORK' in x['opening_ranges'])
            self.assertEqual(datetime.fromtimestamp(london['time']/1000,timezone.utc).hour,expected[0])
            self.assertEqual(datetime.fromtimestamp(ny['time']/1000,timezone.utc).hour,expected[1]+1)

    def test_causal_regime_has_warmup_and_efficiency_bounds(self):
        f=build_features(bars())
        self.assertTrue(all(not x['ready'] for x in f[:199]))
        self.assertTrue(f[199]['ready'])
        self.assertTrue(all(0<=x['efficiency']<=1.00000000001 for x in f if x['efficiency'] is not None))


if __name__=='__main__':unittest.main()
