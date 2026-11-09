"""Nine fixed experimental hypotheses and a fail-closed selector; all opt-in."""
from dataclasses import dataclass, asdict
import hashlib
import json
from math import ceil, floor, isfinite
from pathlib import Path

FAMILIES = ('S1','S2','S3','S4','S5','S6','S7','S8','S9','S10')
NAMES = dict(zip(FAMILIES, ('Trend pullback','Sweep reversal','Acceptance retest','Compression breakout','Failed breakout reversal','VWAP reversion','London/NY opening range','Momentum continuation','Confirmed swing failure','Validated regime selector')))


@dataclass(frozen=True)
class StrategyParams:
    stop_buffer_atr: float = .12
    min_stop_atr: float = .4
    max_stop_atr: float = 3.5
    max_bar_atr: float = 3.0
    max_chase_atr: float = .75
    efficiency_min: float = .25
    adx_min: float = 20
    confirmation_body_atr: float = .25
    setup_age: int = 3
    retest_age: int = 5
    retest_tolerance_atr: float = .2
    compression_ratio: float = .75
    compression_width_atr: float = 6
    vwap_deviation_sd: float = 2
    require_htf: bool = False
    sessions: tuple = ()

    def __post_init__(self):
        for k,v in asdict(self).items():
            if isinstance(v,(int,float)) and not isinstance(v,bool) and (not isfinite(v) or v<0):
                raise ValueError(f'Invalid strategy parameter {k}')
        if not 0<self.min_stop_atr<self.max_stop_atr or self.setup_age<1 or self.retest_age<1:
            raise ValueError('Invalid stop limits or setup age')
        if any(s not in ('LONDON','NEW_YORK') for s in self.sessions):
            raise ValueError('Unknown session')


def stop_round(value, side, tick):
    return (floor(value/tick+1e-9) if side==1 else ceil(value/tick-1e-9))*tick


def validated_selection(manifest, data_start):
    """Evidence-qualified research routing, not production deployment permission."""
    if not manifest:
        return {}, 'NO_INDEPENDENT_VALIDATION'
    try:
        obj=json.loads(Path(manifest).read_text(encoding='utf-8')) if isinstance(manifest,(str,Path)) else manifest
        if obj['status']!='INDEPENDENTLY_VALIDATED' or obj['independent_holdout'] is not True or obj['holdout_end_ms']>=data_start:
            return {}, 'INVALID_OR_OVERLAPPING_VALIDATION'
        evidence=obj['evidence']
        if not evidence or any(hashlib.sha256(Path(e['path']).read_bytes()).hexdigest()!=e['sha256'] for e in evidence):
            return {}, 'UNVERIFIED_VALIDATION_ARTIFACT'
        choices=obj['regime_strategies']
        if any(s not in FAMILIES[:9] for group in choices.values() for s in group):
            return {}, 'INVALID_SELECTOR_FAMILY'
        return choices, None
    except (KeyError,TypeError,ValueError,OSError):
        return {}, 'INVALID_VALIDATION_MANIFEST'


class CandidateLibrary:
    def __init__(self, params=None, mintick=.001):
        params=params or {}
        unknown=set(params)-set(FAMILIES)
        if unknown:
            raise ValueError(f'Unknown family parameters: {unknown}')
        self.params={s:StrategyParams(**params.get(s,{})) for s in FAMILIES}
        self.states={}
        self.mintick=mintick

    def evaluate(self, family, side, i, bars, features):
        p=self.params[family]
        b,f=bars[i],features[i]
        previous=bars[i-1] if i else None
        pf=features[i-1] if i else None
        decision={'signal_id':f"{family}|{side}|{b['time_close']}",'family':family,'side':side,'index':i,'confirmation_time':b['time_close'],'raw_setup':False,'status':'NO_SETUP','reasons':[],'regime':f['regime'],'session':list(f['sessions']),'reference':b['close'],'initial_stop':None,'structure_target':None,'setup_born_index':i,'setup_age':0}
        if family=='S10':
            decision.update(status='BLOCKED',reasons=['S10_REQUIRES_VALIDATED_ROUTING'])
            return decision
        if family=='S6' and not f['vwap_available']:
            decision.update(status='BLOCKED',reasons=['TRUE_VOLUME_AND_VERIFIED_BAR_VWAP_UNAVAILABLE'])
            return decision
        if family=='S7' and not f['session_timezone_available']:
            decision.update(status='BLOCKED',reasons=['IANA_TIMEZONE_DATABASE_UNAVAILABLE'])
            return decision
        if not f['ready'] or previous is None:
            decision.update(status='REJECTED',reasons=['WARMUP_OR_CRITICAL_FEATURE_MISSING'])
            return decision
        atr=f['atr']
        key=(family,side)
        state=self.states.get(key)
        terminated=None
        if state:
            if f['gap']:
                terminated='SETUP_TIME_GAP'
            elif i-state['born']>state['max_age']:
                terminated='SETUP_EXPIRED'
            elif (b['close']-state['anchor'])*side<0:
                terminated='STRUCTURAL_INVALIDATION'
            if terminated:
                del self.states[key]
                state=None
        raw=False
        structure=f['range3_low'] if side==1 else f['range3_high']
        obstacle=f['pivot_high'] if side==1 else f['pivot_low']
        extra=[]
        body=(b['close']-b['open'])*side
        candle=body>=p.confirmation_body_atr*atr and (f['close_location']>=.65 if side==1 else f['close_location']<=.35)
        trend=f['direction']==side and f['adx']>=p.adx_min and f['efficiency']>=p.efficiency_min
        if family=='S1':
            pullback=(previous['low']<=pf['ema20'] and previous['close']<=pf['ema20']) if side==1 else (previous['high']>=pf['ema20'] and previous['close']>=pf['ema20'])
            raw=pullback and (b['close']-(previous['high'] if side==1 else previous['low']))*side>0
            extra=[(trend,'DIRECTIONAL_CONTEXT'),(candle,'CONFIRMATION_CANDLE')]
            structure=min(previous['low'],b['low']) if side==1 else max(previous['high'],b['high'])
        elif family in ('S2','S9'):
            if state:
                raw=(b['close']-state['trigger'])*side>0 and i>state['born']
                structure=state['anchor']
                extra=[(candle,'CONFIRMATION_CANDLE')]
            elif terminated is None:
                level=(f['range20_low'] if side==1 else f['range20_high']) if family=='S2' else (pf['pivot_low'] if side==1 else pf['pivot_high'])
                wick=f['lower_wick'] if side==1 else f['upper_wick']
                sweep=level is not None and ((b['low']<level<b['close']) if side==1 else (b['high']>level>b['close'])) and wick>=.25*f['range']
                if sweep:
                    self.states[key]={'born':i,'anchor':b['low'] if side==1 else b['high'],'trigger':b['high'] if side==1 else b['low'],'max_age':p.setup_age if family=='S2' else 2}
        elif family=='S3':
            if state:
                structure=state['anchor']
                if state['phase']=='ACCEPT':
                    if (b['close']-state['level'])*side>0 and i==state['born']+1:
                        state.update(phase='RETEST',accepted=i,max_age=1+p.retest_age)
                    else:
                        terminated='ACCEPTANCE_FAILED'
                        del self.states[key]
                elif i>state['accepted']:
                    touch=(b['low']<=state['level']+p.retest_tolerance_atr*atr and b['low']>=state['level']-p.retest_tolerance_atr*atr) if side==1 else (b['high']>=state['level']-p.retest_tolerance_atr*atr and b['high']<=state['level']+p.retest_tolerance_atr*atr)
                    raw=touch and (b['close']-state['level'])*side>0
                    extra=[(candle,'CONFIRMATION_CANDLE'),((b['close']-state['level'])*side<=p.max_chase_atr*atr,'CHASE_LIMIT')]
            elif terminated is None:
                level=f['range20_high'] if side==1 else f['range20_low']
                if level is not None and (b['close']-level)*side>0 and (previous['close']-level)*side<=0:
                    self.states[key]={'born':i,'level':level,'trigger':level,'anchor':structure,'phase':'ACCEPT','max_age':1+p.retest_age}
        elif family=='S4':
            level=f['range20_high'] if side==1 else f['range20_low']
            raw=level is not None and (b['close']-level)*side>0 and (previous['close']-level)*side<=0
            compressed=pf['atr_ratio'] is not None and pf['atr_ratio']<p.compression_ratio and pf['range20_high'] is not None and pf['range20_high']-pf['range20_low']<=p.compression_width_atr*pf['atr']
            extra=[(compressed,'PRIOR_COMPRESSION'),(candle,'CONFIRMATION_CANDLE'),(f['range']>=atr,'EXPANSION_REQUIRED'),((b['close']-level)*side<=p.max_chase_atr*atr if level is not None else False,'CHASE_LIMIT')]
        elif family=='S5':
            if state:
                raw=(b['close']-state['level'])*side>0 and i>state['born']
                structure=state['anchor']
                extra=[(candle,'CONFIRMATION_CANDLE')]
            elif terminated is None:
                # A breakout in the opposite direction, followed on a later close by failure.
                level=f['range20_low'] if side==1 else f['range20_high']
                if level is not None and (b['close']-level)*side<0 and (previous['close']-level)*side>=0:
                    self.states[key]={'born':i,'level':level,'trigger':level,'anchor':b['low'] if side==1 else b['high'],'max_age':p.setup_age}
        elif family=='S6':
            v,sd=f['vwap'],f['vwap_sd']
            pv,psd=pf['vwap'],pf['vwap_sd']
            raw=pv is not None and psd is not None and sd>0 and psd>0 and (previous['close']-pv)*side<-p.vwap_deviation_sd*psd and (b['close']-v)*side>=-p.vwap_deviation_sd*sd
            extra=[(f['regime']=='RANGE','RANGE_CONTEXT'),(candle,'CONFIRMATION_CANDLE')]
            structure=min(previous['low'],b['low']) if side==1 else max(previous['high'],b['high'])
            obstacle=v
        elif family=='S7':
            matches=[]
            for name,opening in f['opening_ranges'].items():
                level=opening['high'] if side==1 else opening['low']
                if (b['close']-level)*side>0 and (previous['close']-level)*side<=0:
                    matches.append((name,opening))
            raw=bool(matches)
            if raw:
                name,opening=sorted(matches)[0]
                structure=opening['low'] if side==1 else opening['high']
                decision['opening_session']=name
            extra=[(candle,'CONFIRMATION_CANDLE')]
        elif family=='S8':
            level=f['range3_high'] if side==1 else f['range3_low']
            raw=(b['close']-level)*side>0 and (previous['close']-level)*side<=0
            extra=[(trend,'DIRECTIONAL_CONTEXT'),(f['efficiency']>=max(.35,p.efficiency_min),'EFFICIENCY'),(pf['body']<=.5*pf['atr'],'PRIOR_PAUSE'),(body>=.5*atr,'DISPLACEMENT'),(abs(b['close']-f['ema20'])<=2*atr,'EXHAUSTION_DISTANCE')]
        if state:
            decision.update(setup_born_index=state['born'],setup_age=i-state['born'])
        decision['raw_setup']=raw
        if terminated:
            decision.update(status='REJECTED',reasons=[terminated])
            return decision
        if not raw:
            decision.update(status='WATCH' if key in self.states else 'NO_SETUP',reasons=['WAITING_CONFIRMATION' if key in self.states else 'NO_RAW_TRIGGER'])
            return decision
        # Consume triggered setups even when a context/risk check rejects them.
        self.states.pop(key,None)
        stop=stop_round(structure-side*p.stop_buffer_atr*atr,side,self.mintick) if structure is not None else None
        risk=(b['close']-stop)*side if stop is not None else None
        checks=[(not f['gap'],'TIME_GAP_ON_CONFIRMATION'),(f['range']<=p.max_bar_atr*atr,'EXCESSIVE_BAR'),(not p.sessions or any(s in p.sessions for s in f['sessions']),'SESSION'),(not p.require_htf or f['h1']['bias']==side,'CONFIRMED_H1_ALIGNMENT'),(risk is not None and p.min_stop_atr*atr<=risk<=p.max_stop_atr*atr and risk>=self.mintick,'STRUCTURAL_STOP_RANGE')]+extra
        decision.update(initial_stop=stop,structure_target=obstacle,reasons=[reason for ok,reason in checks if not ok])
        decision['status']='CONFIRMED' if not decision['reasons'] else 'REJECTED'
        return decision
