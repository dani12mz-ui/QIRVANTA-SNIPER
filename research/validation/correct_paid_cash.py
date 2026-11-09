"""Registered 90-run accounting correction; prove all preexisting fills unchanged."""
import gzip
import json
from datetime import datetime, timezone

from research.backtesting.engine import BacktestConfig, run_backtest
from research.data.aggregate import aggregate_m15_to_m30
from research.data.loader import load_bars
from research.validation.protocol import ROOT, REPORTS, sha

REGISTRATION_SHA='29147fbeb2093257cc9e22df83269528a29521f6e9a2d65766762d33f0cbf8cf'
ARCHIVE=REPORTS/'method_history/paid_cash_precorrection'


def projection(new, old):
    """Discard ONLY newly added keys; every old nested economic value must match."""
    if isinstance(old,dict):
        return {k:projection(new[k],v) for k,v in old.items()}
    if isinstance(old,list):
        if len(new)!=len(old):
            raise ValueError('List length changed')
        return [projection(n,o) for n,o in zip(new,old)]
    return new


def run():
    if sha(REPORTS/'paid_cash_correction_registration.json')!=REGISTRATION_SHA:
        raise ValueError('Correction registration changed')
    old_freeze=json.loads((ARCHIVE/'candidate_source_freeze.json').read_text('utf-8'))
    hashes={p.replace('\\','/'):h for p,h in old_freeze['source_sha256'].items()}
    for name in ('research/features/causal.py','research/strategies/library.py'):
        if sha(ROOT/name)!=hashes[name]:
            raise ValueError('Signal code changed without authorization')
    files=('research/data/loader.py','research/data/aggregate.py','research/features/causal.py','research/strategies/library.py','research/backtesting/engine.py','research/validation/statistics.py')
    frozen={'created_utc':datetime.now(timezone.utc).isoformat(),'registration_sha256':REGISTRATION_SHA,'source_sha256':{p:sha(ROOT/p) for p in files},'old_engine_sha256':sha(ARCHIVE/'engine_before.txt'),'old_loader_sha256':sha(ARCHIVE/'loader_before.txt'),'scope':'ENTRY_FEE_PAID_CASH_AND_INPUT_TIMESTAMP_CORRECTION_NO_SIGNAL_CHANGE'}
    freeze_path=REPORTS/'paid_cash_source_freeze.json'
    if freeze_path.exists():
        raise FileExistsError('Correction already started; preserve outputs before a documented rerun')
    freeze_path.write_text(json.dumps(frozen,indent=2)+'\n','utf-8')
    bars=load_bars()
    m30,manifest=aggregate_m15_to_m30(bars)
    original_manifest=json.loads((ROOT/'research/data/m30_manifest.json').read_text('utf-8'))
    if manifest!=original_manifest:
        raise ValueError('Derived data changed')
    compared=[]
    final_reports=[]
    for filename in ('experiment_results.json','m30_results.json'):
        report=json.loads((ARCHIVE/filename).read_text('utf-8'))
        for trial in report['trials']:
            journal_name=trial['journal'].replace('\\','/').split('/')[-1]
            old_path=ARCHIVE/'candidate_journals'/journal_name
            if sha(old_path)!=trial['journal_sha256']:
                raise ValueError('Archived journal changed')
            old=json.loads(gzip.decompress(old_path.read_bytes()))
            new=run_backtest(m30 if filename=='m30_results.json' else bars,BacktestConfig(**old['config']))
            for section in ('config','config_sha256','dataset','decisions','plans','events','equity_closed_cash','blocked_families'):
                if projection(new[section],old[section])!=old[section]:
                    raise ValueError('Unauthorized change to '+section+' in '+trial['trial']['id'])
            for key,value in old['summary'].items():
                if key!='all_realized_net_cash' and new['summary'][key]!=value:
                    raise ValueError('Unexpected old summary change: '+key)
            unpaid=sum(p['unallocated_entry_fee'] for p in old['plans'])
            expected=old['summary']['all_realized_net_cash']-unpaid
            if abs(new['summary']['all_realized_net_cash']-expected)>1e-7:
                raise ValueError('Paid cash delta is not exactly unallocated entry fees')
            checks={'trial_id':trial['trial']['id'],'signals_plans_fills_exact':True,'data_exact':True,'complete_net_exact':True,'closed_part_equity_exact':True,'old_closed_part_net':old['summary']['all_realized_net_cash'],'new_paid_cash_net':new['summary']['all_realized_net_cash'],'expected_unallocated_fee_delta':-unpaid,'old_closed_part_dd':old['summary']['closed_cash_drawdown'],'new_paid_cash_dd':new['summary']['cash_paid_drawdown'],'old_journal_sha256':trial['journal_sha256']}
            current=REPORTS/'candidate_journals'/journal_name
            current.write_bytes(gzip.compress(json.dumps(new,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode('utf-8'),compresslevel=6,mtime=0))
            trial['summary']=new['summary']
            trial['journal_sha256']=sha(current)
            trial['accounting_correction']=checks
            checks['new_journal_sha256']=trial['journal_sha256']
            compared.append(checks)
            print(trial['trial']['id'],'PASS_IDENTICAL_SIGNALS_FILLS',round(new['summary']['all_realized_net_cash']-old['summary']['all_realized_net_cash'],9),flush=True)
        report['paid_cash_correction']={'registration_sha256':REGISTRATION_SHA,'source_freeze_sha256':sha(freeze_path),'archived_original_report':str((ARCHIVE/filename).relative_to(ROOT)),'all_old_signals_fills_complete_nets_preserved':True}
        final_reports.append((filename,report))
    if any(sha(ROOT/p)!=digest for p,digest in frozen['source_sha256'].items()):
        raise ValueError('Source changed during correction')
    for filename,report in final_reports:
        (REPORTS/filename).write_text(json.dumps(report,indent=2,ensure_ascii=False,allow_nan=False)+'\n','utf-8')
    result={'schema':'qirvanta.research.accounting_correction_comparison.v1','completed_utc':datetime.now(timezone.utc).isoformat(),'status':'PASS_ALL_90_SIGNALS_AND_FILLS_EXACT','registration_sha256':REGISTRATION_SHA,'trials':compared,'count':len(compared),'correction_scope':'Actual entry commission debited at entry; no completed net or trading decision change under defaults; timestamp input validation preserved actual dataset.'}
    (REPORTS/'paid_cash_correction_comparison.json').write_text(json.dumps(result,indent=2,ensure_ascii=False,allow_nan=False)+'\n','utf-8')
    return result


if __name__=='__main__':run()
