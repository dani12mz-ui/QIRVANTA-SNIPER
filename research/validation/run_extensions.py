"""Run preregistered actual-TF and bounded ablation/perturbation extensions."""
import gzip
import hashlib
import json
from contextlib import nullcontext
from datetime import datetime, timezone

from research.backtesting.engine import BacktestConfig, run_backtest
from research.data.loader import load_bars
from research.strategies.ablation import gate_ablation
from research.validation.common_window import choose_common_window
from research.validation.protocol import ROOT, REPORTS, sha
from research.validation.statistics import block_bootstrap, cash_metrics, holm, sign_flip_p

REGISTRATIONS={'actual_tf':'57b0802da29a5414051dd096a0253b4046889efc19a8749edf3cc44f1d7a5e9b','ablation':'ba132d93fc093777e6a0bd5f28666548b882093598b80b94f8499c46cc298dcf'}


def native_cohorts(sources, start, end):
    reports={}
    for source in sources:
        path=ROOT/source['native_audit']
        if sha(path)!=source['native_audit_sha256']:
            raise ValueError('Native audit changed')
        audit=json.loads(path.read_text('utf-8'))
        if audit['status']!='PASS_OBSERVED_JOURNAL' or audit['issues']:
            raise ValueError('Native audit not reconciled')
        cohort=[c for c in audit['campaigns'] if c['entry_time'] is not None and start<=c['entry_time']<end]
        complete=sorted((c for c in cohort if c['state']=='COMPLETE' and c['last_exit_time']<end),key=lambda c:(c['entry_time'],c['plan_id']))
        censored=[c for c in cohort if c not in complete]
        carry=[c for c in audit['campaigns'] if c['entry_time'] is not None and c['entry_time']<start and (c['last_exit_time'] is None or c['last_exit_time']>=start)]
        reports[source['timeframe']]={'full_loaded_history_metrics':audit['complete_campaign_metrics'],'full_loaded_history_native_dd':audit['native_equity_max_drawdown'],'cohort_metrics':cash_metrics([c['realized_net_cash'] for c in complete]),'cohort_net_r':sum(c['net_r'] for c in complete if c['net_r'] is not None),'complete_cohort_ids':[c['plan_id'] for c in complete],'end_censored_ids':[c['plan_id'] for c in censored],'prewindow_carryover_ids':[c['plan_id'] for c in carry],'scope':'Actual native full-history run then entry-cohort selection, not native restart at window; prewindow occupancy may differ from flat-start Python; no forced closes or native equity DD invented for cohort'}
    return reports


def add_variant_metadata(result, trial, frozen):
    variant={'trial_id':trial['id'],'engine_sha256':frozen['economic_source_sha256']['research/backtesting/engine.py'],'library_sha256':frozen['economic_source_sha256']['research/strategies/library.py'],'config_sha256':result['config_sha256'],'ablation':{'family':trial['family'],'removed_gate':trial.get('remove_gate'),'adapter_sha256':frozen['ablation_adapter_sha256']} if trial.get('remove_gate') else None}
    variant['composite_sha256']=hashlib.sha256(json.dumps(variant,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    result['research_variant']=variant
    return result


def summarize(result, trial):
    complete=sorted((p for p in result['plans'] if p['status']=='COMPLETE'),key=lambda p:(p['entry_time'],p['plan_id']))
    rs=[p['net_r'] for p in complete if p['net_r'] is not None]
    path=REPORTS/'candidate_journals'/(trial['id']+'.json.gz')
    if path.exists():
        raise FileExistsError('Preserve previous extension journal: '+trial['id'])
    path.write_bytes(gzip.compress(json.dumps(result,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode('utf-8'),compresslevel=6,mtime=0))
    return {'trial':trial,'status':'BLOCKED_NO_VALID_INPUTS' if result['blocked_families'] else 'ZERO_COMPLETED_TRADES' if not complete else 'EXPLORATORY_RESULT','summary':result['summary'],'blocked_families':result['blocked_families'],'variant_identity':result['research_variant'],'campaign_metrics':cash_metrics([p['net_cash'] for p in complete]),'mean_r_uncertainty':block_bootstrap(rs),'sign_flip_p':sign_flip_p(rs),'journal':path.relative_to(ROOT).as_posix(),'journal_sha256':sha(path),'ablation_altered_decisions':sum('ablation_removed_gate' in d for d in result['decisions'])}


def run():
    registrations={}
    for name,digest in REGISTRATIONS.items():
        path=REPORTS/(name+'_registration.json')
        if sha(path)!=digest:
            raise ValueError('Registration changed: '+name)
        registrations[name]=json.loads(path.read_text('utf-8'))
        if (REPORTS/(name+'_results.json')).exists():
            raise FileExistsError('Extension result already exists')
    provenance=json.loads((REPORTS/'final_reproduction_manifest.json').read_text('utf-8'))
    economic={p:h for p,h in provenance['source_sha256'].items() if p!='research/validation/statistics.py'}
    if any(sha(ROOT/p)!=h for p,h in economic.items()):
        raise ValueError('Frozen economic source changed')
    frozen={'created_utc':datetime.now(timezone.utc).isoformat(),'registration_sha256':REGISTRATIONS,'economic_source_sha256':economic,'ablation_adapter_sha256':sha(ROOT/'research/strategies/ablation.py'),'runner_sha256':sha(__file__),'common_window_helper_sha256':sha(ROOT/'research/validation/common_window.py'),'statistics_sha256':sha(ROOT/'research/validation/statistics.py')}
    (REPORTS/'extension_source_freeze.json').write_text(json.dumps(frozen,indent=2)+'\n','utf-8')
    tfreg=registrations['actual_tf']
    data={}
    for source in tfreg['sources']:
        path=ROOT/source['local_csv']
        if sha(path)!=source['csv_sha256']:
            raise ValueError('Actual timeframe input changed')
        data[source['timeframe']]=load_bars(path)
    window=choose_common_window(data)
    if window!=tfreg['common_window'] or window['status']!='AVAILABLE':
        raise ValueError('Shared data-only window changed or unavailable')
    data={tf:[b for b in bars if b['time_close']<=window['end']] for tf,bars in data.items()}
    actual=[]
    for trial in tfreg['trials']:
        config=BacktestConfig(enabled_families=(trial['family'],),trade_start_ms=window['start'],trade_end_ms=window['end'])
        result=add_variant_metadata(run_backtest(data[trial['timeframe']],config),trial,frozen)
        actual.append(summarize(result,trial))
        print(trial['id'],result['summary']['complete_plans'],round(result['summary']['net_cash'],6),flush=True)
    report={'schema':'qirvanta.research.actual_tf_results.v1','registration_sha256':REGISTRATIONS['actual_tf'],'source_freeze_sha256':sha(REPORTS/'extension_source_freeze.json'),'completed_utc':datetime.now(timezone.utc).isoformat(),'common_window':window,'sources':tfreg['sources'],'trials':actual,'native_cohorts':native_cohorts(tfreg['sources'],window['start'],window['end']),'native_parity':False,'untouched_holdout':False,'champion_promoted':False,'limitations':['Actual supplied datasets; raw OHLC kept local because redistribution rights not established','Shared dates selected by data availability/HTF warmup, not return','Different fixed bar-count horizons and next-open latency across TF','Native cohorts inherit prior occupancy, Python starts flat; no paired native improvement claim','No forced terminal close; open/pending visible; S6/S10 blocked']}
    (REPORTS/'actual_tf_results.json').write_text(json.dumps(report,indent=2,ensure_ascii=False,allow_nan=False)+'\n','utf-8')
    ablations=[]
    original_bars=load_bars()
    for trial in registrations['ablation']['trials']:
        config=BacktestConfig(enabled_families=(trial['family'],),strategy_params={trial['family']:trial['parameter']})
        context=gate_ablation(trial['family'],trial['remove_gate']) if trial['remove_gate'] else nullcontext()
        with context:
            result=add_variant_metadata(run_backtest(original_bars,config),trial,frozen)
        ablations.append(summarize(result,trial))
        print(trial['id'],result['summary']['complete_plans'],round(result['summary']['net_cash'],6),flush=True)
    report={'schema':'qirvanta.research.ablation_results.v1','registration_sha256':REGISTRATIONS['ablation'],'source_freeze_sha256':sha(REPORTS/'extension_source_freeze.json'),'completed_utc':datetime.now(timezone.utc).isoformat(),'trials':ablations,'champion_promoted':False,'limitations':['Target families selected after positive initial M15 results, not independent hypothesis confirmation','Ablation removes exactly one declared component in isolated runtime adapter, all other reasons and setup consumption retained','Higher count or P&L never authorizes changing production filters','Composite strategy identity includes adapter plus configuration; base config hash alone is insufficient']}
    (REPORTS/'ablation_results.json').write_text(json.dumps(report,indent=2,ensure_ascii=False,allow_nan=False)+'\n','utf-8')
    combined=[]
    for name in ('experiment_results.json','m30_results.json','actual_tf_results.json','ablation_results.json'):
        combined+=json.loads((REPORTS/name).read_text('utf-8'))['trials']
    adjusted=holm([t['sign_flip_p'] for t in combined])
    joined={'schema':'qirvanta.research.multiple_testing.v1','count':len(combined),'accounting_reruns_counted_as_new':False,'scope':'All129 registered mission trials, prior external/posthoc ideas not included; p-values descriptive not confirmatory','trials':[{'id':t['trial']['id'],'p':t['sign_flip_p'],'holm_adjusted_p_all_129':p} for t,p in zip(combined,adjusted)],'significant_count_05':sum(p<.05 for p in adjusted),'champion_promoted':False}
    (REPORTS/'all_129_multiple_testing.json').write_text(json.dumps(joined,indent=2)+'\n','utf-8')
    return joined


if __name__=='__main__':run()
