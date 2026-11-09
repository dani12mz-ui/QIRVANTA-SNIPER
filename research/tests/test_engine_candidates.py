import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from research.strategies.library import CandidateLibrary, validated_selection


def fixture(rows):
    bars=[dict(zip(('open','high','low','close'),r),time=1704067200000+i*900000,time_close=1704067200000+(i+1)*900000) for i,r in enumerate(rows)]
    features=[]
    for b in bars:
        o,h,l,c=(b[k] for k in ('open','high','low','close'))
        features.append({'ready':True,'regime':'UPTREND','sessions':['LONDON'],'session_timezone_available':True,'gap':False,'atr':1.,'atr_ratio':1.,'adx':25.,'efficiency':.5,'direction':1,'body':abs(c-o),'range':h-l,'close_location':(c-l)/(h-l) if h>l else .5,'lower_wick':min(o,c)-l,'upper_wick':h-max(o,c),'ema20':100.,'h1':{'bias':1},'range3_low':99.5,'range3_high':101.,'range20_low':99.,'range20_high':101.,'pivot_low':99.,'pivot_high':104.,'opening_ranges':{'LONDON':{'low':99.,'high':101.}},'vwap_available':False,'vwap':None,'vwap_sd':None})
    return bars,features


def mirror(bars,features):
    b=copy.deepcopy(bars);f=copy.deepcopy(features)
    for x in b:
        x.update(open=200-x['open'],close=200-x['close'],high=200-x['low'],low=200-x['high'])
    for x in f:
        x['direction']=-1;x['h1']['bias']=-1
        x['regime']='DOWNTREND' if x['regime']=='UPTREND' else x['regime']
        if x['vwap'] is not None:x['vwap']=200-x['vwap']
        x['ema20']=200-x['ema20'];x['close_location']=1-x['close_location']
        x['lower_wick'],x['upper_wick']=x['upper_wick'],x['lower_wick']
        for high,low in [('range3_high','range3_low'),('range20_high','range20_low'),('pivot_high','pivot_low')]:
            x[high],x[low]=200-x[low],200-x[high]
        for state in x['opening_ranges'].values():state['high'],state['low']=200-state['low'],200-state['high']
    return b,f


class CandidateTests(unittest.TestCase):
    def test_s1_trend_pullback_buy_sell(self):
        b,f=fixture([(100,100.2,99.5,99.8),(100,101,99.9,100.8)])
        for side,data in [(1,(b,f)),(-1,mirror(b,f))]:
            result=CandidateLibrary().evaluate('S1',side,1,*data)
            self.assertEqual(result['status'],'CONFIRMED',result)

    def test_s2_sweep_requires_separate_confirmation_and_s9_pivot_failure(self):
        b,f=fixture([(100,100.2,99.7,100),(99.4,100,98.8,99.6),(99.8,100.5,99.7,100.4)])
        for family in ('S2','S9'):
            for side,data in [(1,(b,f)),(-1,mirror(b,f))]:
                lib=CandidateLibrary()
                self.assertEqual(lib.evaluate(family,side,1,*data)['status'],'WATCH')
                self.assertEqual(lib.evaluate(family,side,2,*data)['status'],'CONFIRMED')

    def test_s3_acceptance_then_later_retest(self):
        b,f=fixture([(100,100.3,99.8,100),(100.7,101.3,100.5,101.2),(101.1,101.4,101,101.3),(101.1,101.6,101,101.5)])
        for side,data in [(1,(b,f)),(-1,mirror(b,f))]:
            lib=CandidateLibrary()
            self.assertEqual(lib.evaluate('S3',side,1,*data)['status'],'WATCH')
            self.assertEqual(lib.evaluate('S3',side,2,*data)['status'],'WATCH')
            self.assertEqual(lib.evaluate('S3',side,3,*data)['status'],'CONFIRMED')

    def test_s4_requires_prior_compression(self):
        b,f=fixture([(100,100.3,99.8,100),(100.4,101.3,100,101.2)])
        f[0]['atr_ratio']=.6
        for side,data in [(1,(b,f)),(-1,mirror(b,f))]:
            self.assertEqual(CandidateLibrary().evaluate('S4',side,1,*data)['status'],'CONFIRMED')
        f[0]['atr_ratio']=1.2
        self.assertIn('PRIOR_COMPRESSION',CandidateLibrary().evaluate('S4',1,1,b,f)['reasons'])

    def test_s5_failed_breakout(self):
        b,f=fixture([(100,100.3,99.8,100),(100.9,101.6,100.8,101.5),(101.2,101.3,100.6,100.7)])
        for side,data in [(-1,(b,f)),(1,mirror(b,f))]:
            lib=CandidateLibrary()
            self.assertEqual(lib.evaluate('S5',side,1,*data)['status'],'WATCH')
            self.assertEqual(lib.evaluate('S5',side,2,*data)['status'],'CONFIRMED')

    def test_s6_verified_vwap_gate_then_range_reentry(self):
        b,f=fixture([(97.8,98,97,97.5),(98,98.6,97.9,98.5)])
        for x in f:x.update(vwap_available=True,vwap=100,vwap_sd=1,regime='RANGE')
        for side,data in [(1,(b,f)),(-1,mirror(b,f))]:
            self.assertEqual(CandidateLibrary().evaluate('S6',side,1,*data)['status'],'CONFIRMED')
        f[1]['vwap_available']=False
        self.assertEqual(CandidateLibrary().evaluate('S6',1,1,b,f)['status'],'BLOCKED')

    def test_s7_frozen_opening_range_and_s8_momentum_pause(self):
        b,f=fixture([(100,100.2,99.8,100),(100.5,101.3,100.4,101.2)])
        for family in ('S7','S8'):
            for side,data in [(1,(b,f)),(-1,mirror(b,f))]:
                self.assertEqual(CandidateLibrary().evaluate(family,side,1,*data)['status'],'CONFIRMED')
        f[1]['opening_ranges']={}
        self.assertEqual(CandidateLibrary().evaluate('S7',1,1,b,f)['status'],'NO_SETUP')

    def test_setup_expiry_and_gap_do_not_reuse_stale_trigger(self):
        b,f=fixture([(100,100.2,99.7,100),(99.4,100,98.8,99.6),(99.8,100.5,99.7,100.4)])
        lib=CandidateLibrary();lib.evaluate('S2',1,1,b,f)
        f[2]['gap']=True
        self.assertIn('SETUP_TIME_GAP',lib.evaluate('S2',1,2,b,f)['reasons'])
        self.assertNotIn(('S2',1),lib.states)
        f[2]['gap']=False
        lib.states[('S2',1)]={'born':-5,'anchor':98.8,'trigger':100,'max_age':3}
        self.assertIn('SETUP_EXPIRED',lib.evaluate('S2',1,2,b,f)['reasons'])

    def test_s10_fails_without_verified_independent_evidence(self):
        self.assertTrue(validated_selection(None,1000)[1])
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'evidence.txt';path.write_text('independent reviewed synthetic fixture')
            manifest={'status':'INDEPENDENTLY_VALIDATED','independent_holdout':True,'holdout_end_ms':999,'evidence':[{'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}],'regime_strategies':{'UPTREND':['S1']}}
            self.assertEqual(validated_selection(manifest,1000),({'UPTREND':['S1']},None))
            manifest['holdout_end_ms']=1000
            self.assertTrue(validated_selection(manifest,1000)[1])
            manifest['holdout_end_ms']=999;manifest['evidence'][0]['sha256']='bad'
            self.assertTrue(validated_selection(manifest,1000)[1])


if __name__=='__main__':unittest.main()
