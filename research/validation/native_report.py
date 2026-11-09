"""Enrich immutable native campaigns; OHLC supplies excursion bounds, not fills."""
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from research.data.loader import load_bars
from research.models.logistic import evaluate
from research.validation.protocol import ROOT, REPORTS, SEED, sha
from research.validation.statistics import (block_bootstrap, cash_metrics, chronological_split,
    quantile, sequence_stress, sign_flip_p, walk_forward)


def utc(ms):
    return datetime.fromtimestamp(ms/1000, timezone.utc)


def sessions(ms):
    result = []
    try:
        for name, zone, begin, end in [('LONDON','Europe/London',8*60,16*60+30), ('NEW_YORK','America/New_York',9*60+30,16*60)]:
            local = utc(ms).astimezone(ZoneInfo(zone))
            if local.weekday() < 5 and begin <= local.hour*60+local.minute < end:
                result.append(name)
    except ZoneInfoNotFoundError:
        return 'TIMEZONE_DATABASE_UNAVAILABLE'
    return '+'.join(result) or 'OUTSIDE_DECLARED_LONDON_NY_WINDOWS'


def excursion_bounds(campaign, bars):
    entry, side = campaign['actual_entry'], campaign['side']
    first = campaign['entry_bar_index']
    last = max(r['exit_bar_index'] for r in campaign['exit_rows'])
    if not 0 <= first <= last < len(bars) or bars[first]['time'] != campaign['entry_time'] or bars[last]['time'] != campaign['last_exit_time']:
        return {'status': 'UNRESOLVED_TIMESTAMP_OR_INDEX_ALIGNMENT'}
    # Fill prices and complete interior bars are definitely visited while some
    # position remains. Boundary-bar extremes may occur outside holding time.
    known = [entry] + [r['exit_price'] for r in campaign['exit_rows']]
    interior = [p for b in bars[first+1:last] for p in (b['high'], b['low'])]
    envelope = [p for b in bars[first:last+1] for p in (b['high'], b['low'])]
    def bounds(values):
        return max(0, max((p-entry)*side for p in values)), max(0, max((entry-p)*side for p in values))
    low_mfe, low_mae = bounds(known+interior)
    high_mfe, high_mae = bounds(known+envelope)
    risk_price = campaign['initial_risk_cash']/campaign['submitted_quantity']
    return {'status': 'PRICE_EXCURSION_BOUNDS_NOT_WEIGHTED_CAMPAIGN_EQUITY', 'mfe_price_lower': low_mfe, 'mfe_price_upper': high_mfe, 'mae_price_lower': low_mae, 'mae_price_upper': high_mae,
            'mfe_r_lower': low_mfe/risk_price, 'mfe_r_upper': high_mfe/risk_price,
            'mae_r_lower': low_mae/risk_price, 'mae_r_upper': high_mae/risk_price,
            'recorded_duration_hours': (campaign['last_exit_time']-campaign['entry_time'])/3600000,
            'duration_bars_inclusive': last-first+1,
            'exact_intrabar_duration_available': False,
            'quantity_weighted_cash_excursion_available': False,
            'first_index': first, 'last_index': last,
            'precision': 'OHLC exported to at most 8 decimals; native fill slippage may lie outside bar envelope; endpoint extremes are bounds'}


def fixed_fill_stress(campaigns, fee_multiplier, extra_ticks_per_fill, mintick=.001, pointvalue=1):
    values = []
    for c in campaigns:
        turnover = c['submitted_quantity'] + sum(abs(r['size']) for r in c['exit_rows'])
        values.append(c['realized_net_cash'] - (fee_multiplier-1)*c['realized_commission_cash'] - turnover*extra_ticks_per_fill*mintick*pointvalue)
    return {'fee_multiplier': fee_multiplier, 'additional_adverse_ticks_per_fill': extra_ticks_per_fill, 'fixed_executions': True, 'interpretation': 'penalty applied to actual native executions; no fill reselection, no fabricated spread history', **cash_metrics(values)}


def label_events(campaigns):
    events = []
    for c in campaigns:
        m = c['submitted_metadata']
        # All inputs frozen at signal close. Actual entry, future outcomes and
        # future regime labels MUST NOT become predictive features.
        x = [m['efficiency'], float(m['local_aligned']), float(m['countertrend']),
             m['side'], abs(m['reference']-m['initial_sl'])/m['atr'],
             float(m['family']==2), float(m['family']==3)]
        events.append({'id': c['plan_id'], 'start': m['submitted_time'],
                       # Exit timestamps are bar-open stamps; use bar-close bound.
                       'end': c['last_exit_time']+900000,
                       'x': x, 'y': int(c['realized_net_cash']>0)})
    return events


def model_report(campaigns):
    events = label_events(campaigns)
    split = chronological_split(events)
    folds = []
    for fold in walk_forward(events):
        train = fold['train']
        cut = int(.8*len(train))
        fit, calibration = train[:cut], train[cut:]
        if calibration:
            fit = [e for e in fit if e['end'] < calibration[0]['start']-900000]
        folds.append({'test_ids': [e['id'] for e in fold['test']], 'purged_n': fold['purged_n'], **evaluate(fit,calibration,fold['test'])})
    return {'status': 'EXPLORATORY_OLD_DATA_NO_DEPLOYMENT', 'labels': 'one complete native campaign profitable after actual commission',
            'feature_schema': ['signal_efficiency20','signal_local_aligned','signal_countertrend','side','planned_stop_distance_over_signal_atr','family_FAST','family_REVERSAL'],
            'feature_availability': 'submitted_metadata at confirmed signal close',
            'seed': SEED, 'split_ids': {k:[e['id'] for e in split[k]] for k in ('train','validation','test','purged')},
            'chronological_60_20_20': evaluate(split['train'],split['validation'],split['test']),
            'rolling_expanding_walk_forward': folds,
            'limitations': ['72 campaign selected sample; not all no-trade bars', 'later labels already inspected before this mission', 'no untouched test or independent calibration test', 'no position sizing or Pine integration', 'complex models deferred: inadequate independent sample, no supported point-in-time macro/features'],
            'events': events}


def build_report():
    audit_path = ROOT / 'diagnostics/accounting/B-ALL.audit.json'
    audit = json.loads(audit_path.read_text('utf-8'))
    if audit['status'] != 'PASS_OBSERVED_JOURNAL' or audit['issues'] or not audit['cash_reconciliation']['matches']:
        raise ValueError('native journal is not reconciled')
    campaigns = sorted(audit['campaigns'], key=lambda c:(c['entry_time'],c['entry_id']))
    if any(c['state'] != 'COMPLETE' for c in campaigns):
        raise ValueError('incomplete campaigns require separate accounting')
    bars = load_bars()
    enriched = []
    groups = {key:defaultdict(list) for key in ('entry_week_UTC','entry_month_UTC','entry_session_DST','family','side','native_coarse_regime','trend_relation','signal_efficiency_bin')}
    observed_lower, observed_upper = set(), set()
    for c in campaigns:
        bound = excursion_bounds(c,bars)
        if bound['status'] == 'PRICE_EXCURSION_BOUNDS_NOT_WEIGHTED_CAMPAIGN_EQUITY':
            observed_lower.update(range(bound['first_index']+1,bound['last_index']))
            observed_upper.update(range(bound['first_index'],bound['last_index']+1))
        dt = utc(c['entry_time'])
        categories = {'entry_week_UTC':dt.strftime('%G-W%V'), 'entry_month_UTC':dt.strftime('%Y-%m'), 'entry_session_DST':sessions(c['entry_time']), 'family':str(c['family']), 'side':str(c['side']), 'native_coarse_regime':c['regime'], 'trend_relation':c['trend_relation'], 'signal_efficiency_bin':'GE_0.25' if c['submitted_metadata']['efficiency'] >= .25 else 'LT_0.25'}
        for key,label in categories.items():
            groups[key][label].append(c['realized_net_cash'])
        enriched.append({'plan_id':c['plan_id'], 'net_cash':c['realized_net_cash'], 'net_r':c['net_r'], 'actual_qty':c['submitted_quantity'], 'actual_entry':c['actual_entry'], 'categories':categories, 'excursion_bounds':bound})
    cash = [c['realized_net_cash'] for c in campaigns]
    rs = [c['net_r'] for c in campaigns if c['net_r'] is not None]
    valid_bounds = [e['excursion_bounds'] for e in enriched if e['excursion_bounds']['status'].startswith('PRICE_')]
    realized_month = defaultdict(list)
    for c in campaigns:
        for r in c['exit_rows']:
            realized_month[utc(r['exit_time']).strftime('%Y-%m')].append(r['net_cash'])
    result = {'schema':'qirvanta.research.native_enrichment.v1','status':'DESCRIPTIVE_ACTUAL_NATIVE_CAMPAIGNS',
              'native_audit_sha256':sha(audit_path),'data_sha256':sha(ROOT/'diagnostics/protocol/datasets/B-ALL-FULL/bars.csv'),
              'native_summary':audit['complete_campaign_metrics'],'actual_cost_reconciliation':audit['cash_reconciliation'],
              'native_equity_drawdown':audit['native_equity_max_drawdown'],
              'uncertainty':{'cash_mean':block_bootstrap(cash),'net_r_mean':block_bootstrap(rs),'sign_flip_positive_mean_p':sign_flip_p(rs),'pvalue_limit':'exploratory sign symmetry/independence null is not established for dependent selected trades; no confirmatory claim'},
              'sequence_stress':sequence_stress(cash),
              'excursions':{'resolved_campaigns':len(valid_bounds),'median_price_MFE_R_lower':quantile([b['mfe_r_lower'] for b in valid_bounds],.5),'median_price_MFE_R_upper':quantile([b['mfe_r_upper'] for b in valid_bounds],.5),'median_price_MAE_R_lower':quantile([b['mae_r_lower'] for b in valid_bounds],.5),'median_price_MAE_R_upper':quantile([b['mae_r_upper'] for b in valid_bounds],.5),'median_recorded_duration_hours':quantile([b['recorded_duration_hours'] for b in valid_bounds],.5),'max_recorded_duration_hours':max(b['recorded_duration_hours'] for b in valid_bounds)},
              'exposure':{'covered_market_bar_fraction_lower':len(observed_lower)/len(bars),'covered_market_bar_fraction_upper':len(observed_upper)/len(bars),'lower_covered_bars':len(observed_lower),'upper_touched_bars':len(observed_upper),'total_observed_bars':len(bars),'scope':'price exposure while any original quantity remains; excludes unavailable session gaps; no exact intrabar or quantity-weighted exposure'},
              'groups':{key:{name:cash_metrics(values) for name,values in sorted(group.items())} for key,group in groups.items()},
              'realized_cash_by_exit_month':{key:{'partial_rows':len(values),'net_cash':sum(values),'scope':'cash realization; not independent campaign win-rate'} for key,values in sorted(realized_month.items())},
              'fixed_fill_cost_stress':[fixed_fill_stress(campaigns,m,t) for m,t in ((1,0),(1.5,1),(2,2))],
              'campaigns':enriched,
              'unavailable':['exact intrabar MAE/MFE and quantity-weighted cash path','executable bid/ask spread history','broker swaps and financing','independent holdout','point-in-time macro/news features'],
              'other_timeframes':'Later actual M5/M30 captures and shared-window experiment are separate in actual_tf_results.json; this report remains the original M15 native72 cohort',
              'volume_revision_note':'A new fixed-cutoff TradingView reproduction matches native campaign objects but revises 33 historical volume cells; locked research CSV remains original SHA7015...; neither version validates executable/centralized volume or VWAP.'}
    model = model_report(campaigns)
    REPORTS.mkdir(parents=True,exist_ok=True)
    for name,value in [('native_enrichment.json',result),('logistic_validation.json',model)]:
        (REPORTS/name).write_text(json.dumps(value,indent=2,ensure_ascii=False,allow_nan=False)+'\n','utf-8')
    return {'campaigns':len(campaigns),'resolved_bounds':len(valid_bounds),'native_net':sum(cash),'mean_r':mean(rs),'model':model['chronological_60_20_20']}


if __name__ == '__main__':
    print(json.dumps(build_report(),ensure_ascii=False))
