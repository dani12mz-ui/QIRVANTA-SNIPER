#!/usr/bin/env python3
"""Causal, exploratory level-grid benchmark. Not the author's unspecified trading strategy.
Python 3.11+, standard library only. CLI: python benchmark_369.py --data data --out results
"""
from __future__ import annotations
import argparse, csv, hashlib, json, math, random, statistics, sys
from collections import Counter, defaultdict
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

WARMUP=250
RISK_USD=25.0
COSTS={'base': .35+2*.002+2*.175, 'stress': .70+2*.005+2*.2625}
METHODS=('369','fixed12','fixed15','fixed18','cycleA','cycleB','atr2','structure','369_round')

@dataclass(frozen=True)
class Bar:
    time:int; time_close:int; open:float; high:float; low:float; close:float
@dataclass(frozen=True)
class Anchor:
    aid:int; known:int; origin:int; inward:int; price:float; atr:float; step:float
    structure:tuple[float,...]

def utc(t:int)->str:
    return datetime.fromtimestamp(t/1000,timezone.utc).isoformat()
def sha(p:Path)->str:
    return hashlib.sha256(p.read_bytes()).hexdigest()
def digital_root(n:int)->int:
    if not isinstance(n,int) or n<=0: raise ValueError('positive whole-dollar reference required')
    return 1+(n-1)%9

def step369(p:float, rounding:str='floor')->float:
    n=math.floor(p) if rounding=='floor' else math.floor(p+0.5)
    r=digital_root(n)
    return float(12+3*((r-1)%3))

def load_bars(path:Path)->tuple[list[Bar],dict]:
    bars=[]
    for r in csv.DictReader(path.open(encoding='utf-8-sig',newline='')):
        b=Bar(int(r['time']),int(r['time_close']),*(float(r[k]) for k in ('open','high','low','close')))
        if not all(math.isfinite(v) for v in (b.open,b.high,b.low,b.close)): raise ValueError('nonfinite OHLC')
        if min(b.open,b.close)<b.low-1e-8 or max(b.open,b.close)>b.high+1e-8 or b.high<b.low: raise ValueError('invalid OHLC')
        if bars and b.time<=bars[-1].time: raise ValueError('duplicate or unsorted times')
        if b.time_close<=b.time: raise ValueError('invalid interval')
        bars.append(b)
    periods={b.time_close-b.time for b in bars}
    if len(periods)!=1 or len(bars)<=WARMUP+30: raise ValueError('insufficient/mixed history')
    gap_idx=[i for i in range(1,len(bars)) if bars[i].time!=bars[i-1].time_close]
    return bars,{'file':path.name,'sha256':sha(path),'bars':len(bars),'first_open':utc(bars[0].time),
                 'last_close':utc(bars[-1].time_close),'evaluation_start':utc(bars[WARMUP].time),
                 'evaluation_bars':len(bars)-WARMUP,'frame_minutes':next(iter(periods))//60000,
                 'gap_count':len(gap_idx),'gap_times':[utc(bars[i].time) for i in gap_idx]}

def atr14(bars:list[Bar])->list[float|None]:
    tr=[];out=[];prior=None
    for i,b in enumerate(bars):
        v=b.high-b.low if i==0 else max(b.high-b.low,abs(b.high-bars[i-1].close),abs(b.low-bars[i-1].close))
        tr.append(v)
        if i<13: out.append(None)
        elif i==13: prior=sum(tr)/14;out.append(prior)
        else: prior=(13*prior+v)/14;out.append(prior)
    return out

def make_anchors(bars:list[Bar], atr:list[float|None])->list[Anchor]:
    out=[]; pivots=[]
    for i in range(6,len(bars)):
        # Require seven actual consecutive source bars; never manufacture weekend bars.
        if any(bars[j].time!=bars[j-1].time_close for j in range(i-5,i+1)): continue
        p=i-3
        others=[j for j in range(i-6,i+1) if j!=p]
        new=[]
        if all(bars[p].high>bars[j].high for j in others):new.append((-1,bars[p].high))
        if all(bars[p].low<bars[j].low for j in others):new.append((1,bars[p].low))
        for inward,price in new:
            levels=[]
            for old in reversed(pivots):
                if (old-price)*inward>1e-8 and all(abs(old-x)>.0005 for x in levels):
                    levels.append(old)
                if len(levels)==4:break
            if i>=WARMUP and atr[i] and atr[i]>0:
                out.append(Anchor(len(out),i,p,inward,price,float(atr[i]),step369(price),tuple(levels)))
            pivots.append(price)
    return out

def grid(a:Anchor,method:str,custom:dict[int,float]|None=None)->list[float]:
    if method=='structure':return list(a.structure)
    if custom is not None:d=custom[a.aid]
    elif method=='369':d=a.step
    elif method=='369_round':d=step369(a.price,'round')
    elif method.startswith('fixed'):d=float(method[5:])
    elif method=='cycleA':d={12.:15.,15.:18.,18.:12.}[a.step]
    elif method=='cycleB':d={12.:18.,15.:12.,18.:15.}[a.step]
    elif method=='atr2':d=2*a.atr
    else:raise ValueError(method)
    return [round(a.price+a.inward*d*k,6) for k in range(1,5)]

def events_for(bars:list[Bar],atr:list[float|None],anchors:list[Anchor],method:str,
               custom:dict[int,float]|None=None)->tuple[list[dict],dict]:
    by_known=defaultdict(list)
    for a in anchors:by_known[a.known].append(a)
    active={};events=[];formed=0;replaced=0;expired=0
    for i in range(WARMUP,len(bars)):
        b=bars[i]
        if i and b.time!=bars[i-1].time_close:active.clear()
        current=[]
        for inward,(a,levels,seen) in list(active.items()):
            if i-a.known>=24:
                expired+=1;del active[inward];continue
            ar=atr[i-1]
            if not ar or ar<=0:continue
            tol=.05*ar
            for k,lev in enumerate(levels):
                if k in seen:continue
                if b.low<=lev+tol and b.high>=lev-tol:
                    seen.add(k)
                    prior=bars[i-1].close
                    side=1 if prior>lev+tol else -1 if prior<lev-tol else 0
                    reject=bool(side and side*(b.close-lev)>=.10*ar and side*(b.close-b.open)>=.25*ar)
                    current.append({'bar':i,'time_close':b.time_close,'anchor_id':a.aid,'anchor_known':a.known,
                                    'origin':a.origin,'level_no':k+1,'level':lev,'inward':a.inward,'side':side,
                                    'atr':ar,'rejection':reject,'ambiguous_multi':False})
        # Distinct levels touched by the same bar have an unknown intrabar sequence.
        count=len({round(e['level'],3) for e in current})
        for e in current:e['ambiguous_multi']=count>1
        events.extend(current)
        # New anchors become usable only on the NEXT source bar.
        for a in by_known[i]:
            if a.inward in active:replaced+=1
            levels=grid(a,method,custom);formed+=len(levels)
            active[a.inward]=(a,levels,set())
    stats={'anchors':len(anchors),'levels_created':formed,'first_touches':len(events),
           'directional_touches':sum(e['side']!=0 for e in events),
           'rejections':sum(e['rejection'] for e in events),'multi_level_touches':sum(e['ambiguous_multi'] for e in events),
           'expired_grids':expired,'replaced_grids':replaced}
    return events,stats

def post_touch(bars:list[Bar],events:list[dict],horizon:int)->dict:
    # Descriptive future response from the next midpoint open, not source-author trade rules.
    out=Counter()
    for e in events:
        i=e['bar'];side=e['side'];ar=e['atr']
        if side==0:out['unknown_approach']+=1;continue
        if i+horizon>=len(bars):out['censored']+=1;continue
        if bars[i+1].time!=bars[i].time_close:out['gap_after_touch']+=1;continue
        entry=bars[i+1].open;win=entry+side*.5*ar;loss=entry-side*.5*ar
        result='neither'
        for j in range(i+1,i+horizon+1):
            b=bars[j]
            w=b.high>=win if side>0 else b.low<=win
            l=b.low<=loss if side>0 else b.high>=loss
            if l:result='adverse_or_ambiguous';break
            if w:result='favorable_first';break
        out[result]+=1
    n=sum(out[k] for k in ('favorable_first','adverse_or_ambiguous','neither'))
    return {**dict(out),'evaluable':n,'favorable_fraction':out['favorable_first']/n if n else None}

def one_trade(bars:list[Bar],e:dict)->dict|None:
    i=e['bar'];start=i+1
    if i+12>=len(bars) or bars[start].time!=bars[i].time_close:return None
    side=e['side'];risk=e['atr'];entry=bars[start].open
    stop=entry-side*risk;target=entry+side*1.5*risk
    end=i+12;exit_px=bars[end].close;reason='TIME';ambiguous=False
    for j in range(start,end+1):
        b=bars[j]
        if side*(b.open-stop)<=0:
            end=j;exit_px=b.open;reason='STOP_GAP';break
        if side*(b.open-target)>=0:
            end=j;exit_px=target;reason='TARGET_GAP_CONSERVATIVE';break
        s=b.low<=stop if side>0 else b.high>=stop
        t=b.high>=target if side>0 else b.low<=target
        if s:
            end=j;exit_px=stop;reason='STOP';ambiguous=t;break
        if t:
            end=j;exit_px=target;reason='TARGET';break
    gross=side*(exit_px-entry)/risk
    return {**e,'entry_bar':start,'exit_bar':end,'entry_utc':utc(bars[start].time),
            'exit_utc':utc(bars[end].time_close),'entry_mid':entry,'exit_mid':exit_px,'stop_mid':stop,
            'target_mid':target,'risk_price':risk,'units':RISK_USD/risk,'gross_R':gross,
            'reason':reason,'same_bar_ambiguity':ambiguous,
            'base_net_R':gross-COSTS['base']/risk,'stress_net_R':gross-COSTS['stress']/risk}

def ledger(bars:list[Bar],events:list[dict])->tuple[list[dict],dict]:
    grouped=defaultdict(list)
    for e in events:
        if e['rejection']:grouped[e['bar']].append(e)
    trades=[];occupied=-1;reasons=Counter()
    for i,es in sorted(grouped.items()):
        if any(e['ambiguous_multi'] for e in es):reasons['multi_level_ambiguous']+=1;continue
        if len({e['side'] for e in es})!=1:reasons['conflicting_sides']+=1;continue
        # Duplicate identical levels at two anchors are one economic event.
        e=min(es,key=lambda x:(-x['anchor_known'],x['anchor_id'],x['level_no']))
        if i+1<=occupied:reasons['position_occupied']+=1;continue
        tr=one_trade(bars,e)
        if tr is None:reasons['gap_or_insufficient_forward_bars']+=1;continue
        occupied=tr['exit_bar'];trades.append(tr)
    return trades,dict(reasons)

def metrics(bars:list[Bar],trades:list[dict],costname:str)->dict:
    vals=[t[costname+'_net_R'] for t in trades];gains=sum(v for v in vals if v>0);losses=-sum(v for v in vals if v<0)
    cumulative=0.;peak=0.;closeddd=0.
    for v in vals:cumulative+=v;peak=max(peak,cumulative);closeddd=max(closeddd,peak-cumulative)
    realized=0.;peak=0.;mtmdd=0.
    for tr in trades:
        for j in range(tr['entry_bar'],tr['exit_bar']+1):
            equity=realized+(tr['side']*(bars[j].close-tr['entry_mid'])/tr['risk_price']-COSTS[costname]/tr['risk_price'])
            if j==tr['exit_bar']:equity=realized+tr[costname+'_net_R']
            peak=max(peak,equity);mtmdd=max(mtmdd,peak-equity)
        realized+=tr[costname+'_net_R']
    n=len(vals)
    midpoint=WARMUP+(len(bars)-WARMUP)//2
    return {'trades':n,'wins':sum(v>0 for v in vals),'losses':sum(v<0 for v in vals),
            'win_rate':sum(v>0 for v in vals)/n if n else None,
            'gross_R':sum(t['gross_R'] for t in trades),'net_R':sum(vals),'net_USD':RISK_USD*sum(vals),
            'mean_net_R':sum(vals)/n if n else None,'PF':gains/losses if losses>0 else None,
            'closed_drawdown_R':closeddd,'close_mark_to_market_drawdown_R':mtmdd,
            'total_cost_R':sum(COSTS[costname]/t['risk_price'] for t in trades),
            'buy_net_R':sum(t[costname+'_net_R'] for t in trades if t['side']>0),
            'sell_net_R':sum(t[costname+'_net_R'] for t in trades if t['side']<0),
            'early_half_net_R':sum(t[costname+'_net_R'] for t in trades if t['entry_bar']<midpoint),
            'late_half_net_R':sum(t[costname+'_net_R'] for t in trades if t['entry_bar']>=midpoint),
            'same_bar_stop_first_cases':sum(t['same_bar_ambiguity'] for t in trades)}

def percentile(a:list[float],p:float)->float:
    a=sorted(a);k=(len(a)-1)*p;lo=math.floor(k);hi=math.ceil(k)
    return a[lo]*(hi-k)+a[hi]*(k-lo) if hi!=lo else a[lo]

def daily_bootstrap(bars:list[Bar],a:list[dict],b:list[dict],reps=2000)->dict:
    days=sorted({utc(x.time)[:10] for x in bars[WARMUP:]});d=defaultdict(float)
    for t in a:d[t['exit_utc'][:10]]+=t['stress_net_R']
    for t in b:d[t['exit_utc'][:10]]-=t['stress_net_R']
    values=[d[day] for day in days];rng=random.Random(369369);samples=[];n=len(values)
    for _ in range(reps):
        sample=[]
        while len(sample)<n:
            st=rng.randrange(n);sample.extend([values[st],values[(st+1)%n]])
        samples.append(sum(sample[:n]))
    return {'method':'circular 2-trading-day block bootstrap; descriptive, small-sample',
            'days':n,'replications':reps,'observed_stress_delta_R':sum(values),
            'ci95_delta_R':[percentile(samples,.025),percentile(samples,.975)]}

def self_tests()->dict:
    checks={}
    checks['source_4023']=(step369(4023)==18 and [4023-18*k for k in range(1,5)]==[4005,3987,3969,3951])
    checks['root_table']=all(step369(float(n))==expected for n,expected in {1:12,4:12,7:12,2:15,5:15,8:15,3:18,6:18,9:18}.items())
    checks['mod3_identity']=all(step369(n)==12+3*((n-1)%3) for n in range(1,10001))
    checks['fraction_preserved']=grid(Anchor(0,10,7,-1,4155.57,5,step369(4155.57),()),'369')==[4137.57,4119.57,4101.57,4083.57]
    checks['rounding_sensitivity']=step369(4023.6)!=step369(4023.6,'round')
    checks['stress_costs_higher']=COSTS['stress']>COSTS['base']
    checks['zero_invalid']=False
    try:digital_root(0)
    except ValueError:checks['zero_invalid']=True
    # Synthetic input tests test code only, not strategy profitability.
    bs=[Bar(i*900000,(i+1)*900000,100.,100.5,99.5,100.) for i in range(20)]
    bs[2]=Bar(1800000,2700000,100.,102.,98.,100.)
    e={'bar':1,'side':1,'atr':1.,'level':100.,'rejection':True,'anchor_id':0,'level_no':1}
    t=one_trade(bs,e)
    checks['same_bar_stop_first']=bool(t and t['gross_R']==-1 and t['same_bar_ambiguity'])
    bs[2]=Bar(1800000,2700000,100.,100.5,99.5,100.)
    bs[3]=Bar(2700000,3600000,98.,98.5,97.5,98.)
    t=one_trade(bs,e);checks['stop_gap_worse']=bool(t and t['gross_R']==-2 and t['reason']=='STOP_GAP')
    checks['tail_censored']=one_trade(bs,dict(e,bar=15)) is None
    checks['one_slot']=len(ledger(bs,[dict(e,ambiguous_multi=False,anchor_known=0),dict(e,bar=2,ambiguous_multi=False,anchor_known=0)])[0])==1
    if not all(checks.values()):raise AssertionError(checks)
    return checks

def run(data:Path,out:Path)->dict:
    out.mkdir(parents=True,exist_ok=True);test_results=self_tests();allres={'status':'EXPLORATORY_NOT_SOURCE_STRATEGY','unit_tests':test_results,'costs_price_per_unit_roundtrip':COSTS,'frames':{}}
    for frame in (15,5,30):
        bars,meta=load_bars(data/f'OANDA_XAUUSD_M{frame}.csv');atr=atr14(bars);anchors=make_anchors(bars,atr)
        res={'data':meta,'anchor_count':len(anchors),'step_counts':dict(Counter(a.step for a in anchors)),
             'rounding_changes':sum(step369(a.price,'round')!=a.step for a in anchors),'methods':{}}
        trades_by={}
        for method in METHODS:
            ev,stats=events_for(bars,atr,anchors,method);tr,blocked=ledger(bars,ev);trades_by[method]=tr
            res['methods'][method]={'event_statistics':stats,'blocked':blocked,
                'touch_response':{str(h):post_touch(bars,ev,h) for h in (3,6,12)},
                'base':metrics(bars,tr,'base'),'stress':metrics(bars,tr,'stress')}
            if method in ('369','fixed15','structure','369_round'):
                (out/f'M{frame}_{method}_events.json').write_text(json.dumps(ev,indent=2))
                (out/f'M{frame}_{method}_trades.json').write_text(json.dumps(tr,indent=2))
        # Prefix causality: computed events must not change when future bars are absent.
        base_ev,_=events_for(bars,atr,anchors,'369');prefixchecks=[]
        for n in (len(bars)//2,len(bars)*3//4,len(bars)-20):
            bb=bars[:n];aa=atr14(bb);an=make_anchors(bb,aa);ev,_=events_for(bb,aa,an,'369')
            ok=ev==[e for e in base_ev if e['bar']<n];prefixchecks.append({'bars':n,'matches':ok})
            if not ok:raise AssertionError(('prefix changed',frame,n))
        res['prefix_tests']=prefixchecks
        res['versus_fixed15_bootstrap']=daily_bootstrap(bars,trades_by['369'],trades_by['fixed15'])
        # Placebo reassigns the observed distance multiset within anchor side. No best seed selection.
        perms=[]
        for seed in range(200):
            rng=random.Random(369000+seed);assignment={}
            for side in (-1,1):
                group=[a for a in anchors if a.inward==side];ds=[a.step for a in group];rng.shuffle(ds)
                assignment.update({a.aid:d for a,d in zip(group,ds)})
            ev,_=events_for(bars,atr,anchors,'369',assignment);tr,_=ledger(bars,ev)
            perms.append({'seed':seed,'trades':len(tr),'stress_net_R':sum(t['stress_net_R'] for t in tr)})
        vals=[p['stress_net_R'] for p in perms];actual=res['methods']['369']['stress']['net_R']
        res['permutation']={'iterations':len(vals),'observed_stress_R':actual,
            'null_median_R':statistics.median(vals),'null_5_95_R':[percentile(vals,.05),percentile(vals,.95)],
            'one_sided_randomization_p':(1+sum(v>=actual-1e-12 for v in vals))/(len(vals)+1),
            'interpretation':'Exploratory superiority versus random step assignment; not a future-performance probability.'}
        (out/f'M{frame}_permutations.json').write_text(json.dumps(perms,indent=2))
        (out/f'M{frame}_anchors.json').write_text(json.dumps([asdict(a) for a in anchors],indent=2))
        # Stronger cost must never improve a fixed-ledger result.
        assert all(res['methods'][m]['stress']['net_R']<=res['methods'][m]['base']['net_R']+1e-10 for m in METHODS)
        allres['frames'][str(frame)]=res
    allres['code_sha256']=sha(Path(__file__))
    (out/'RESULTS.json').write_text(json.dumps(allres,indent=2,ensure_ascii=False))
    with (out/'SUMMARY.csv').open('w',newline='') as f:
        w=csv.writer(f);w.writerow(['frame','method','trades','touches','rejections','gross_R','base_net_R','stress_net_R','base_USD','stress_USD','stress_PF','stress_win_rate','stress_close_DD_R'])
        for frame,rr in allres['frames'].items():
            for m,r in rr['methods'].items():
                b=r['base'];s=r['stress'];e=r['event_statistics']
                w.writerow([frame,m,s['trades'],e['first_touches'],e['rejections'],s['gross_R'],b['net_R'],s['net_R'],b['net_USD'],s['net_USD'],s['PF'],s['win_rate'],s['close_mark_to_market_drawdown_R']])
    return allres

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--data',type=Path);p.add_argument('--out',type=Path);p.add_argument('--self-test',action='store_true');a=p.parse_args()
    if a.self_test:print(json.dumps(self_tests(),indent=2));sys.exit(0)
    if a.data is None or a.out is None:p.error('--data and --out required')
    result=run(a.data,a.out)
    print(json.dumps({fr:{m:{'n':r['stress']['trades'],'base':r['base']['net_R'],'stress':r['stress']['net_R']} for m,r in rr['methods'].items()} for fr,rr in result['frames'].items()},indent=2))
