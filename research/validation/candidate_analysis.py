"""Descriptive decompositions of ALL saved trials; never select a champion."""
import gzip
import json
from collections import defaultdict
from datetime import datetime, timezone

from research.validation.protocol import ROOT, REPORTS, sha
from research.validation.statistics import cash_metrics


def stamp(ms):
    return datetime.fromtimestamp(ms/1000,timezone.utc)


def group_metrics(plans, label):
    groups=defaultdict(list)
    for plan in plans:
        groups[label(plan)].append(plan['net_cash'])
    return {key:cash_metrics(values) for key,values in sorted(groups.items())}


def analyze():
    results=json.loads((REPORTS/'experiment_results.json').read_text('utf-8'))
    all_trials=[]
    for t in results['trials']:
        path=ROOT/t['journal']
        if sha(path)!=t['journal_sha256']:
            raise ValueError('Candidate journal hash mismatch')
        data=json.loads(gzip.decompress(path.read_bytes()))
        complete=sorted((p for p in data['plans'] if p['status']=='COMPLETE'),key=lambda p:(p['entry_time'],p['plan_id']))
        # Calendar group definitions are descriptive, after seeing the entire
        # sample. None is used for threshold selection or promotion.
        groups={
            'side':group_metrics(complete,lambda p:str(p['side'])),
            'entry_month_UTC':group_metrics(complete,lambda p:stamp(p['entry_time']).strftime('%Y-%m')),
            'entry_week_UTC':group_metrics(complete,lambda p:stamp(p['entry_time']).strftime('%G-W%V')),
            'signal_regime':group_metrics(complete,lambda p:p['regime']),
            'signal_session_DST':group_metrics(complete,lambda p:'+'.join(p['sessions']) or 'OUTSIDE_WINDOWS'),
            'signal_volatility_ATR_ratio_descriptive':group_metrics(complete,lambda p:'LT_0.8' if (p['features_at_confirmation']['atr_ratio'] or 0)<.8 else 'GT_1.2' if p['features_at_confirmation']['atr_ratio']>1.2 else '0.8_TO_1.2'),
        }
        realized=defaultdict(float)
        for p in data['plans']:
            for row in p['exits']:
                realized[stamp(row['time']).strftime('%Y-%m')]+=row['net_cash']
        open_plans=[{'plan_id':p['plan_id'],'status':p['status'],'remaining_quantity':p['remaining_quantity'],'entry_time':p['entry_time'],'closed_parts_net':p['net_cash'],'cash_paid_net':p['cash_paid_net'],'unallocated_entry_fee':p['unallocated_entry_fee']} for p in data['plans'] if p['status'] in ('OPEN','PENDING')]
        paid_month=defaultdict(float)
        for row in data['equity_cash_paid']:
            paid_month[stamp(row['time']).strftime('%Y-%m')]+=row['cash_change']
        cross_month=[{'plan_id':p['plan_id'],'entry_month':stamp(p['entry_time']).strftime('%Y-%m'),'exit_month':stamp(p['final_exit_time']).strftime('%Y-%m'),'net_cash':p['net_cash']} for p in complete if stamp(p['entry_time']).month!=stamp(p['final_exit_time']).month]
        all_trials.append({'trial_id':t['trial']['id'],'groups':groups,'allocated_partial_net_by_exit_month':dict(sorted(realized.items())),'actual_paid_cash_by_event_month':dict(sorted(paid_month.items())),'realization_scope':'paid cash charges entry fees at entry month; allocated partial net charges both fees at exit month, shown separately; never sum both ledgers','cross_month_campaigns':cross_month,'open_or_pending':open_plans})
    result={'schema':'qirvanta.research.candidate_decomposition.v1','results_sha256':sha(REPORTS/'experiment_results.json'),'status':'DESCRIPTIVE_ALL_ALREADY_OBSERVED_TRIALS_NO_FILTER_SELECTION','timeframe':'M15_ORIGINAL_80_TRIAL_DECOMPOSITION_ONLY','other_timeframes':'Later actual M5/M15/M30 shared-window results are in actual_tf_results.json','volatility_bins':'0.8/1.2 chosen for descriptive reporting after experiment execution, not tested filters or preregistered hypotheses','trials':all_trials}
    (REPORTS/'candidate_decomposition.json').write_text(json.dumps(result,indent=2,ensure_ascii=False,allow_nan=False)+'\n','utf-8')
    return len(all_trials)


if __name__=='__main__': print(analyze())
