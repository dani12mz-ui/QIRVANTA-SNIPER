"""Execute only the immutable, already registered exploratory trial list."""
import gzip
import json
from dataclasses import asdict
from datetime import datetime, timezone

from research.backtesting.engine import BacktestConfig, run_backtest
from research.data.loader import load_bars
from research.strategies.library import StrategyParams
from research.validation.protocol import ROOT, REPORTS, sha
from research.validation.statistics import block_bootstrap, cash_metrics, holm, sign_flip_p

REGISTERED_SHA = '6d422f4384cdcd012330b39fdee444be09d1aee9a984e7f036df0c15aac6b1cd'


def run():
    registration_path = REPORTS/'experiment_registration.json'
    if sha(registration_path) != REGISTERED_SHA:
        raise ValueError('Registration changed; do not execute unregistered trials')
    registration = json.loads(registration_path.read_text('utf-8'))
    frozen_sources = {str(p.relative_to(ROOT)):sha(p) for group in ('data','features','strategies','backtesting','validation','models','labeling') for p in sorted((ROOT/'research'/group).glob('*.py'))}
    frozen = {'recorded_utc':datetime.now(timezone.utc).isoformat(), 'registration_sha256':REGISTERED_SHA, 'strategy_defaults':asdict(StrategyParams()), 'engine_defaults':asdict(BacktestConfig()), 'source_sha256':frozen_sources}
    freeze_path = REPORTS/'candidate_source_freeze.json'
    if freeze_path.exists():
        previous = json.loads(freeze_path.read_text('utf-8'))
        if previous['source_sha256'] != frozen_sources:
            raise ValueError('Sources changed after freeze; preserve prior runs and register a correction before rerunning')
    else:
        freeze_path.write_text(json.dumps(frozen,indent=2,ensure_ascii=False)+'\n','utf-8')
    trials = []
    bars = load_bars()
    journal_dir = REPORTS/'candidate_journals'
    journal_dir.mkdir(exist_ok=True)
    for trial in registration['experiments']:
        multiplier = trial['cost_multiplier']
        config = BacktestConfig(enabled_families=(trial['family'],), exit_variant=trial['exit_variant'], entry_delay_bars=trial['entry_delay_bars'], commission_per_unit=.175*multiplier, slippage_ticks=2*multiplier)
        result = run_backtest(bars,config)
        complete = sorted((p for p in result['plans'] if p['status']=='COMPLETE'),key=lambda p:(p['entry_time'],p['plan_id']))
        rs = [p['net_r'] for p in complete if p['net_r'] is not None]
        serialized = json.dumps(result,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode('utf-8')
        journal = journal_dir/(trial['id']+'.json.gz')
        journal.write_bytes(gzip.compress(serialized,compresslevel=6,mtime=0))
        trial_result = {'trial':trial,'status':'BLOCKED_NO_VALID_INPUTS' if result['blocked_families'] else 'ZERO_COMPLETED_TRADES' if not complete else 'EXPLORATORY_RESULT',
                        'engine':result['implementation'],'config_sha256':result['config_sha256'],'summary':result['summary'], 'blocked_families':result['blocked_families'],
                        'campaign_metrics':cash_metrics([p['net_cash'] for p in complete]),
                        'mean_r_uncertainty':block_bootstrap(rs), 'sign_flip_p':sign_flip_p(rs),
                        'journal':str(journal.relative_to(ROOT)), 'journal_sha256':sha(journal)}
        trials.append(trial_result)
        print(trial['id'],trial_result['status'],len(complete),round(result['summary']['net_cash'],6),flush=True)
    adjusted = holm([t['sign_flip_p'] for t in trials])
    for trial,p in zip(trials,adjusted):
        trial['holm_adjusted_p_all_80'] = p
    report = {'schema':'qirvanta.research.experiments.v1','registration_sha256':REGISTERED_SHA,'source_freeze_sha256':sha(freeze_path),'completed_utc':datetime.now(timezone.utc).isoformat(),'data_sha256':registration['data']['sha256'],'native_parity':False,'untouched_holdout':False,'champion_promoted':False,'trial_count':len(trials),'trials':trials,
              'limitations':['All histories previously inspected; family definitions exploratory, not independently validated','OHLC conservative simulator differs from TradingView broker emulator; no direct native performance improvement claim','80 trials are related; Holm controls formal familywise error only under valid individual null p-values, which dependence/stationarity here do not establish','S6/S10 blocked, zero-trade and all-loss trials are retained','Gzip files contain all decisions, rejected candidates, pending/open/complete plans, partial fills, fees, events and equity; no forced final close','No broker bid/ask, swaps, real executions or point-in-time news; cost multipliers are scenarios']}
    (REPORTS/'experiment_results.json').write_text(json.dumps(report,indent=2,ensure_ascii=False,allow_nan=False)+'\n','utf-8')
    return report


if __name__ == '__main__':
    run()
