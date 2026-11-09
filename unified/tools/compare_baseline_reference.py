"""Identical supplied-candidate replay of baseline/unified shadow reference engines.

This does not run Pine, generate candidates from OHLC, or model TradingView fills.
"""
from dataclasses import asdict
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]

def module(path,name):
    spec=importlib.util.spec_from_file_location(name,path)
    result=importlib.util.module_from_spec(spec);sys.modules[name]=result
    spec.loader.exec_module(result)
    return result

def scenario():
    # Deliberately synthetic: ordinary target, stop+target ambiguity, gap, conflict,
    # opposite directions, model priority, portfolio capacity and filtered context.
    bars=[];candidates=[];start=1735732800000
    for i in range(72):
        o=100.0 if i%6!=1 else (103.0 if i%12==1 else 99.0)
        high=131.0 if i%6==2 else max(o,104.0)
        low=89.0 if i%6==3 else min(o,96.0)
        bars.append(dict(index=i,time=start+i*900000,time_close=start+(i+1)*900000,open=o,high=high,low=low,close=100.0))
        batch=[]
        if i%6==0:
            side=1 if i%12==0 else -1
            family=(i//6)%3+1
            base=dict(id=f'synthetic-{i}-{family}-{side}',family=family,side=side,signal_bar=i,signal_time=bars[-1]['time_close'],reference=100.0,stop=90.0 if side==1 else 110.0,targets=(110.0,120.0,130.0) if side==1 else (90.0,80.0,70.0),atr=10.0,cost_price=.3,countertrend=i%18==0,local_aligned=i%24==0,efficiency_ratio=.4)
            batch.append(base)
            if i%24==0:
                other=dict(base,id=f'synthetic-conflict-{i}',family=2 if family!=2 else 3,side=-side,stop=110.0 if side==1 else 90.0,targets=(90.0,80.0,70.0) if side==1 else (110.0,120.0,130.0))
                batch.append(other)
        candidates.append(batch)
    return bars,candidates

def run(engine,bars,candidates,policy):
    e=engine.Engine(engine.Config(context_policy=policy,max_hold_bars=4))
    admitted=[]
    for bar,plans in zip(bars,candidates):
        admitted.extend(asdict(p) for p in e.process(engine.Bar(**bar),[engine.Plan(**p) for p in plans]))
    return {'candidates_after_eligibility':admitted,'books':{k:{'events':v.events,'trades':v.trades,'ledger':asdict(v.ledger),'remaining':v.remaining,'phase':v.phase} for k,v in e.books.items()}}

def compare():
    baseline=module(ROOT.parent/'refactored/tools/ref_engine.py','qirvanta_baseline_reference')
    unified=module(ROOT/'tools/ref_engine.py','qirvanta_unified_reference')
    bars,candidates=scenario();runs=[]
    for policy in ('БАЗОВ','СЪГЛАСУВАН ТРЕНД — ПРОБЕН'):
        a=run(baseline,bars,candidates,policy);b=run(unified,bars,candidates,policy)
        same=a==b
        runs.append({'policy':policy,'identical':same,'supplied_candidates':sum(map(len,candidates)),
                     'candidate_eligibility_records':len(a['candidates_after_eligibility']),
                     'event_counts':{k:len(v['events']) for k,v in a['books'].items()},
                     'closed_counts':{k:v['ledger']['closed'] for k,v in a['books'].items()},
                     'baseline_result_sha256':hashlib.sha256(json.dumps(a,sort_keys=True).encode()).hexdigest(),
                     'unified_result_sha256':hashlib.sha256(json.dumps(b,sort_keys=True).encode()).hexdigest()})
    return {'status':'PASS_SYNTHETIC_REFERENCE_REPLAY' if all(r['identical'] for r in runs) else 'FAIL',
            'scope':'SYNTHETIC_SUPPLIED_CANDIDATES_NOT_PINE_SIGNAL_OR_NATIVE_FILL_PARITY',
            'identical_data_and_costs':True,'bars':len(bars),'cost_price':.3,'runs':runs,
            'dataset_sha256':hashlib.sha256(json.dumps([bars,candidates],sort_keys=True).encode()).hexdigest(),
            'performance_claim':False}

if __name__=='__main__':
    result=compare();(ROOT/'reports').mkdir(exist_ok=True)
    (ROOT/'reports/synthetic_reference_comparison.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
    print(json.dumps(result,ensure_ascii=False,indent=2));raise SystemExit(result['status']=='FAIL')
