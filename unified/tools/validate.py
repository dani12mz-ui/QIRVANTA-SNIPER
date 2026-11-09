#!/usr/bin/env python3
"""Replay exported Pine CONFIRMED plans on explicit OHLC. Not a signal generator.
No data is downloaded; synthetic fixtures are clearly segregated from market results.
"""
from __future__ import annotations
import argparse, csv, hashlib, json, random, statistics, sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tools.ref_engine import Bar,Plan,Config,Engine

def timestamp(x):
    try:return int(x)
    except (ValueError,TypeError):
        d=datetime.fromisoformat(str(x).replace('Z','+00:00'))
        if d.tzinfo is None:raise ValueError('ISO timestamp must contain timezone')
        return int(d.timestamp()*1000)

def load_bars(path):
    with Path(path).open(encoding='utf-8-sig',newline='') as source:
        rows=list(csv.DictReader(source))
    required={'time','time_close','open','high','low','close'}
    if not rows or not required.issubset(rows[0]):raise ValueError(f'OHLC CSV needs {sorted(required)}')
    bars=[Bar(i,timestamp(r['time']),timestamp(r['time_close']),*[float(r[k]) for k in ['open','high','low','close']]) for i,r in enumerate(rows)]
    for previous,current in zip(bars,bars[1:]):
        if current.time<previous.time_close:raise ValueError('Unordered, overlapping or duplicate bars')
    return bars

def load_events(path):
    rows=[];seen=set()
    for line_no,line in enumerate(Path(path).read_text(encoding='utf-8-sig').splitlines(),1):
        if not line.strip():continue
        start=line.find('{')
        if start<0:raise ValueError(f'No JSON at line {line_no}')
        raw=json.loads(line[start:]);batch=raw.get('events',[raw])
        for e in batch:
            eid=e.get('event_id')
            if not eid:raise ValueError(f'Missing event_id at line {line_no}')
            if eid in seen:raise ValueError(f'Duplicate event id {eid}')
            seen.add(eid);rows.append(e)
    return rows

def plans_by_bar(events,bars):
    index={b.time_close:b.index for b in bars};result={};seen=set()
    for e in events:
        if e.get('event')!='CONFIRMED' or e.get('book')!='CORE':continue
        t=timestamp(e['known_time'])
        if t not in index:raise ValueError('Candidate time not present in OHLC; do not silently drop it')
        if timestamp(e['effective_time'])!=t:raise ValueError('Confirmed candidate must be known on its effective close')
        pid=e['plan_id']
        if pid in seen:raise ValueError('Repeated confirmed plan id')
        seen.add(pid)
        p=Plan(id=pid,family=int(e['family']),side=int(e['side']),signal_bar=index[t],signal_time=t,reference=float(e['reference']),stop=float(e['initial_sl']),targets=tuple(float(e[f'tp{i}']) for i in (1,2,3)),atr=float(e['atr_at_signal']),cost_price=float(e['roundtrip_cost_price']),max_risk_atr=float(e['max_risk_atr']),obstacle=e.get('obstacle'),required_room=float(e.get('required_room_r',0)),countertrend=e.get('countertrend',False),local_aligned=e.get('local_aligned',False),efficiency_ratio=e.get('efficiency_ratio'))
        result.setdefault(index[t],[]).append(p)
    return result

def validate_provenance(events,manifest,bars,config):
    """Check internal event/manifest consistency, not truth of the supplied provenance.

    Synthetic fixtures may omit code hashes because no Pine execution occurred.
    """
    seconds=int(manifest['timeframe_seconds'])
    if seconds not in (300,900,1800,3600,14400):
        raise ValueError('Unsupported manifest timeframe')
    if any(b.time_close-b.time!=seconds*1000 for b in bars):
        raise ValueError('Bar duration does not match manifest timeframe')
    real=manifest['dataset_kind']=='real'
    if real and not events:raise ValueError('Real replay needs an actual exported event log')
    digest=manifest['pine_core_sha256']
    for e in events:
        if e.get('symbol')!=manifest['symbol']:
            raise ValueError('Mixed symbol/provider events')
        if str(e.get('timeframe'))!=str(seconds//60):
            raise ValueError('Event timeframe does not match manifest')
        if (real or 'core_sha256' in e) and e.get('core_sha256')!=digest:
            raise ValueError('Event core_sha256 does not match manifest')
        if real and e.get('schema')!='qirvanta.event.v1':
            raise ValueError('Unsupported or missing event schema')
        if real and e.get('event')=='CONFIRMED' and e.get('book')=='CORE':
            if 'local_aligned' not in e or 'efficiency_ratio' not in e:
                raise ValueError('Missing frozen candidate context')
    inputs=manifest['pine_inputs']
    if 'contextPolicy' in inputs and inputs['contextPolicy']!=config.context_policy:
        raise ValueError('Pine context policy differs from replay configuration')
    if 'minEfficiency' in inputs and inputs['minEfficiency']!=config.min_efficiency:
        raise ValueError('Pine efficiency threshold differs from replay configuration')
    if real and not all(k in inputs for k in ('contextPolicy','minEfficiency')):
        raise ValueError('Real replay requires explicit Pine contextPolicy and minEfficiency')


def metrics(trades):
    rr=[t['net_r'] for t in trades];gross=sum(t['gross_r'] for t in trades);cost=sum(t['cost_r'] for t in trades)
    wins=[r for r in rr if r>1e-10];losses=[r for r in rr if r<-1e-10];equity=peak=dd=0
    for r in rr:equity+=r;peak=max(peak,equity);dd=max(dd,peak-equity)
    return {'closed':len(rr),'positive':len(wins),'negative':len(losses),'zero':len(rr)-len(wins)-len(losses),'gross_r':gross,'cost_r':cost,'net_r':sum(rr),'mean_net_r':statistics.mean(rr) if rr else None,'profit_factor':sum(wins)/(-sum(losses)) if losses else None,'profit_factor_status':'FINITE' if losses else 'NO_LOSSES' if wins else 'NO_DATA','closed_equity_max_drawdown_r':dd,'mean_win_r':statistics.mean(wins) if wins else None,'mean_loss_r':statistics.mean(losses) if losses else None,'mean_holding_bars':statistics.mean(t['hold_bars'] for t in trades) if trades else None}

def block_bootstrap_mean(trades,seed=42,repetitions=1000,block=5):
    rr=[t['net_r'] for t in trades];n=len(rr)
    if n<40:return {'status':'INSUFFICIENT_SAMPLE','minimum':40}
    rng=random.Random(seed);means=[]
    for _ in range(repetitions):
        values=[]
        while len(values)<n:
            start=rng.randrange(n);values.extend(rr[(start+j)%n] for j in range(block))
        means.append(statistics.mean(values[:n]))
    means.sort()
    return {'status':'CONDITIONAL_DESCRIPTIVE_NOT_FUTURE_GUARANTEE','block_length':block,'repetitions':repetitions,'seed':seed,'mean_interval_95':[means[int(.025*repetitions)],means[int(.975*repetitions)]]}

def evaluate(bars,plans,config):
    engine=Engine(config)
    for b in bars:engine.process(b,plans.get(b.index,[]))
    return engine

def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--bars',type=Path);ap.add_argument('--events',type=Path);ap.add_argument('--manifest',type=Path)
    ap.add_argument('--out',type=Path,default=Path('reports/validation_run'))
    ap.add_argument('--train-end');ap.add_argument('--validation-end')
    ap.add_argument('--cost-multipliers',default='1,1.5,2')
    args=ap.parse_args(argv);args.out.mkdir(parents=True,exist_ok=True)
    missing=[name for name in ('bars','events','manifest') if getattr(args,name) is None or not getattr(args,name).is_file()]
    if missing:
        result={'status':'PERFORMANCE_UNVERIFIED_MISSING_DATA','missing':missing,'message':'No invented returns. Supply OHLC, complete exported Pine events, and provenance manifest.'}
        (args.out/'status.json').write_text(json.dumps(result,indent=2),encoding='utf-8');print(json.dumps(result));return 2
    try:
        manifest=json.loads(args.manifest.read_text(encoding='utf-8-sig'));required=('dataset_kind','symbol','provider','timeframe_seconds','pine_core_sha256','pine_inputs','coverage_complete','config')
        if any(k not in manifest for k in required):raise ValueError('Incomplete manifest; see data/MANIFEST_EXAMPLE.json')
        if manifest['dataset_kind'] not in ('real','synthetic'):raise ValueError('dataset_kind must be real or synthetic')
        if manifest['coverage_complete'] is not True:raise ValueError('Incomplete Pine log coverage: comparison is invalid, export complete coverage first')
        if manifest['dataset_kind']=='real':
            digest=manifest['pine_core_sha256']
            if not isinstance(digest,str) or len(digest)!=64 or any(x not in '0123456789abcdef' for x in digest.lower()):
                raise ValueError('Real dataset requires exact SHA-256 core fingerprint')
            if not manifest['pine_inputs']:
                raise ValueError('Real dataset requires the complete Pine input configuration')
        c=Config(**manifest['config']);bars=load_bars(args.bars);events=load_events(args.events)
        validate_provenance(events,manifest,bars,c)
        plans=plans_by_bar(events,bars);engine=evaluate(bars,plans,c)
        results={};trades=[];all_events=[]
        for name,book in engine.books.items():
            family_breakdown={str(f):metrics([t for t in book.trades if t['family']==f]) for f in (1,2,3)}
            summed_count=sum(v['closed'] for v in family_breakdown.values())
            summed_net=sum(v['net_r'] for v in family_breakdown.values())
            if summed_count!=book.ledger.closed or abs(summed_net-book.ledger.net)>1e-8:
                raise ValueError('Per-book family ledger does not reconcile')
            results[name]={**metrics(book.trades),'ledger':asdict(book.ledger),'family_breakdown':family_breakdown,'open_phase':book.phase,'bootstrap':block_bootstrap_mean(book.trades)}
            trades+=book.trades;all_events+=book.events
        segments={}
        if bool(args.train_end)!=bool(args.validation_end):raise ValueError('Provide both chronological split boundaries')
        if args.train_end:
            t1,t2=timestamp(args.train_end),timestamp(args.validation_end)
            if not bars[0].time<t1<t2<bars[-1].time_close:raise ValueError('Splits must be interior and chronological')
            for name,lo,hi in [('train',bars[0].time,t1),('validation',t1,t2),('test',t2,bars[-1].time_close+1)]:
                segments[name]={book:metrics([t for t in obj.trades if lo<=t['signal_time']<hi and t['exit_known_time']<hi]) for book,obj in engine.books.items()}
            segments['method']='Predeclared timestamp boundaries; boundary-crossing trades excluded from segment metrics; carried chronological evidence history; no fitting was performed. This is conditional execution validation, NOT independently proven signal OOS.'
            segments['boundary_crossing_counts']={name:sum(t['signal_time']<cut<=t['exit_known_time'] for t in trades) for name,cut in [('train_end',t1),('validation_end',t2)]}
        stress={}
        from dataclasses import replace
        for mult in (float(v) for v in args.cost_multipliers.split(',')):
            if mult<=0:raise ValueError('Cost multipliers must be positive')
            adjusted={i:[replace(p,cost_price=p.cost_price*mult) for p in pp] for i,pp in plans.items()}
            # Keep same confirmed-candidate cohort. Larger costs may reject an open;
            # do NOT silently regenerate different technical signal rules.
            try:
                stressed=evaluate(bars,adjusted,replace(c,funding_long=c.funding_long*mult,funding_short=c.funding_short*mult))
                stress[str(mult)]={name:metrics(book.trades) for name,book in stressed.books.items()}
            except ValueError as err:
                stress[str(mult)]={'status':'CANDIDATE_RISK_LIMIT_EXCEEDED','detail':str(err),'method':'Higher costs invalidate a supplied plan; rerun Pine with matching costs for new candidate discovery.'}
        status='SYNTHETIC_FUNCTIONAL_ONLY' if manifest['dataset_kind']=='synthetic' else 'CONDITIONAL_EXECUTION_REPLAY_NOT_PINE_PARITY'
        report={'status':status,'manifest':manifest,'inputs_sha256':{str(p.name):hashlib.sha256(p.read_bytes()).hexdigest() for p in (args.bars,args.events,args.manifest)},'books':results,'chronological_segments':segments,'cost_stress':stress,'candidate_count':sum(map(len,plans.values())),'pine_compilation':'NOT_CHECKED','signal_generation':'EXPORTED_PINE_INPUT_NOT_REGENERATED_IN_PYTHON'}
        (args.out/'metrics.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        with (args.out/'trades.jsonl').open('w',encoding='utf-8') as f:
            for t in trades:f.write(json.dumps(t,ensure_ascii=False)+'\n')
        fields=['plan_id','book','family','side','signal_time','modeled_fill_time','modeled_fill_price','initial_sl','exit_known_time','exit_effective_time','exit_price','exit_reason','initial_risk_price','gross_r','cost_r','net_r','hold_bars','evidence']
        with (args.out/'trades.csv').open('w',newline='',encoding='utf-8') as f:
            w=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore');w.writeheader();w.writerows(trades)
        with (args.out/'events.jsonl').open('w',encoding='utf-8') as f:
            for e in all_events:f.write(json.dumps(e,ensure_ascii=False)+'\n')
        print(json.dumps({'status':status,'candidates':report['candidate_count'],'report':str(args.out/'metrics.json')}));return 0
    except (ValueError,KeyError,TypeError,OSError) as err:
        (args.out/'status.json').write_text(json.dumps({'status':'INVALID_INPUT','error':str(err)},ensure_ascii=False,indent=2),encoding='utf-8');print(str(err),file=sys.stderr);return 2
if __name__=='__main__':raise SystemExit(main())
