"""Execute the registered, derived-M30 robustness scenario; preserve M15 runs."""
import gzip
import json
from dataclasses import asdict
from datetime import datetime, timezone

from research.backtesting.engine import BacktestConfig, run_backtest
from research.data.aggregate import aggregate_m15_to_m30
from research.data.loader import load_bars
from research.validation.protocol import ROOT, REPORTS, sha
from research.validation.statistics import block_bootstrap, cash_metrics, holm, sign_flip_p

AMENDMENT_SHA='b3aaf63d393566779f6e08fb1cf28985dcb2d8ede04f67347df5e7ef686b509f'


def run():
    path=REPORTS/'m30_registration_amendment.json'
    if sha(path)!=AMENDMENT_SHA:
        raise ValueError('M30 amendment changed')
    old=json.loads((REPORTS/'candidate_source_freeze.json').read_text('utf-8'))
    hashes={name.replace('\\','/'):value for name,value in old['source_sha256'].items()}
    # Additional statistical/report helpers do not authorize an economic change.
    for name in ('research/data/loader.py','research/features/causal.py','research/strategies/library.py','research/backtesting/engine.py'):
        if sha(ROOT/name)!=hashes[name]:
            raise ValueError('Frozen economic engine changed: '+name)
    frozen={'registered_utc':datetime.now(timezone.utc).isoformat(),'amendment_sha256':AMENDMENT_SHA,'economic_source_freeze_sha256':sha(REPORTS/'candidate_source_freeze.json'),'new_aggregation_sha256':sha(ROOT/'research/data/aggregate.py'),'current_statistics_sha256':sha(ROOT/'research/validation/statistics.py'),'runner_sha256':sha(__file__)}
    (REPORTS/'m30_source_freeze.json').write_text(json.dumps(frozen,indent=2)+'\n','utf-8')
    bars,manifest=aggregate_m15_to_m30(load_bars())
    (ROOT/'research/data/m30_manifest.json').write_text(json.dumps(manifest,indent=2)+'\n','utf-8')
    trials=[]
    for trial in json.loads(path.read_text('utf-8'))['experiments']:
        result=run_backtest(bars,BacktestConfig(enabled_families=(trial['family'],)))
        complete=sorted((p for p in result['plans'] if p['status']=='COMPLETE'),key=lambda p:(p['entry_time'],p['plan_id']))
        rs=[p['net_r'] for p in complete if p['net_r'] is not None]
        journal=REPORTS/'candidate_journals'/(trial['id']+'.json.gz')
        journal.write_bytes(gzip.compress(json.dumps(result,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode('utf-8'),compresslevel=6,mtime=0))
        t={'trial':trial,'status':'BLOCKED_NO_VALID_INPUTS' if result['blocked_families'] else 'ZERO_COMPLETED_TRADES' if not complete else 'EXPLORATORY_RESULT','engine':result['implementation'],'config_sha256':result['config_sha256'],'summary':result['summary'],'blocked_families':result['blocked_families'],'campaign_metrics':cash_metrics([p['net_cash'] for p in complete]),'mean_r_uncertainty':block_bootstrap(rs),'sign_flip_p':sign_flip_p(rs),'journal':str(journal.relative_to(ROOT)),'journal_sha256':sha(journal)}
        trials.append(t)
        print(trial['id'],t['status'],len(complete),round(result['summary']['net_cash'],6),flush=True)
    original=json.loads((REPORTS/'experiment_results.json').read_text('utf-8'))
    combined=original['trials']+trials
    joint=[{'trial_id':t['trial']['id'],'p':t['sign_flip_p'],'holm_adjusted_p_all_90':p} for t,p in zip(combined,holm([t['sign_flip_p'] for t in combined]))]
    report={'schema':'qirvanta.research.m30_experiments.v1','amendment_sha256':AMENDMENT_SHA,'source_freeze_sha256':sha(REPORTS/'m30_source_freeze.json'),'aggregation_manifest_sha256':sha(ROOT/'research/data/m30_manifest.json'),'completed_utc':datetime.now(timezone.utc).isoformat(),'native_m30_history':False,'independent_data':False,'champion_promoted':False,'bars':len(bars),'trials':trials,'combined_multiple_testing':joint,'limitations':['Coarser OHLC derived only from exact contiguous M15 pairs, not fresh broker or native M30','Same numeric bar-count settings double wall-clock hold/cooldown; entry latency and intrabar ambiguity change','Already observed period; exploratory post-original-results amendment, not untouched test','M5 cannot be reconstructed; volume still unverified; S6/S10 stay blocked']}
    (REPORTS/'m30_results.json').write_text(json.dumps(report,indent=2,ensure_ascii=False,allow_nan=False)+'\n','utf-8')
    return report


if __name__=='__main__':run()
