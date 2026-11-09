"""Freeze exploratory research scope BEFORE historical candidate execution.

No strategy is promoted by this registration. Existing data were already inspected.
"""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REPORTS = ROOT / 'research/reports'
SEED = 20261109


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def register():
    REPORTS.mkdir(parents=True, exist_ok=True)
    target = REPORTS / 'experiment_registration.json'
    if target.exists():
        raise FileExistsError('Registration is immutable; append a dated amendment instead')
    baseline = json.loads((ROOT / 'diagnostics/accounting/B-ALL.audit.json').read_text('utf-8'))
    freeze = {
        'schema': 'qirvanta.research.baseline.v1',
        'created_utc': datetime.now(timezone.utc).isoformat(),
        'mission_parent': 'dd1819515b6fe27e2a08bff91c1fbad6bfd7bcc9',
        'champion_source_commit': '48e5cddab17ea831bdc277f17f0406329fdfe39c',
        'champion_source_sha256': baseline['run']['source_sha256'],
        'native_audit_sha256': sha(ROOT / 'diagnostics/accounting/B-ALL.audit.json'),
        'bar_data_sha256': sha(ROOT / 'diagnostics/protocol/datasets/B-ALL-FULL/bars.csv'),
        'baseline_snapshot_sha256': sha(ROOT / 'docs/MISSION_BASELINE.json'),
        'native_campaign_metrics': baseline['complete_campaign_metrics'],
        'native_cash_reconciliation': baseline['cash_reconciliation'],
        'native_equity_max_drawdown': baseline['native_equity_max_drawdown'],
        'untouched_test_available': False,
        'production_change_allowed_by_this_registration': False,
    }
    (REPORTS / 'baseline_freeze.json').write_text(json.dumps(freeze, indent=2, ensure_ascii=False) + '\n', 'utf-8')
    experiments = []
    for family in [f'S{i}' for i in range(1, 11)]:
        experiments.append({'id': family + '-BASE', 'family': family, 'exit_variant': 'PARTIAL_R', 'entry_delay_bars': 1, 'cost_multiplier': 1})
        # Five prechosen mechanism variants and two execution/cost stresses. No best-only reporting.
        for variant in ('FIXED_R', 'STRUCTURAL', 'BREAKEVEN', 'ATR_TRAIL'):
            experiments.append({'id': family + '-' + variant, 'family': family, 'exit_variant': variant, 'entry_delay_bars': 1, 'cost_multiplier': 1})
        experiments.append({'id': family + '-DELAY2', 'family': family, 'exit_variant': 'PARTIAL_R', 'entry_delay_bars': 2, 'cost_multiplier': 1})
        for multiplier in (1.5, 2):
            experiments.append({'id': family + '-COST' + str(multiplier), 'family': family, 'exit_variant': 'PARTIAL_R', 'entry_delay_bars': 1, 'cost_multiplier': multiplier})
    registration = {
        'schema': 'qirvanta.research.registration.v1',
        'registered_utc': datetime.now(timezone.utc).isoformat(),
        'stage': 'EXPLORATORY_ALREADY_OBSERVED_DATA_NOT_CONFIRMATORY',
        'baseline_freeze_sha256': sha(REPORTS / 'baseline_freeze.json'),
        'seed': SEED,
        'data': {'symbol': 'OANDA:XAUUSD', 'timeframe': 'M15', 'bars': 6445, 'start_utc': '2026-08-02T22:00:00Z', 'end_exclusive_utc': '2026-11-09T02:45:00Z', 'sha256': freeze['bar_data_sha256'], 'already_observed': True},
        'fixed_engine_assumptions': {'quantity': 3, 'pointvalue': 1, 'commission_per_unit_per_fill': 0.175, 'slippage_ticks': 2, 'mintick': 0.001, 'max_hold_bars': 48, 'cooldown_bars': 8, 'max_entry_deviation_atr': 0.2},
        'experiments': experiments,
        'hypotheses': [
            'S1-S9 fixed rule families are exploratory implementations, not validated translations of native baseline.',
            'S6 must stay blocked without independent meaningful volume semantics; S10 without independent validation manifest.',
            'Exit, delayed entry and cost variants measure fragility; selection of the best variant is forbidden as a promotion rule.',
            'A causal logistic model using pre-entry native metadata may fail to beat train-prevalence Brier; all failures are retained.'
        ],
        'analysis': {'native_campaign_unit': 'one original entry_id aggregating all partial exits', 'wilson_confidence': 0.95, 'bootstrap': 'circular moving blocks of 5 campaigns, 2000 repetitions, fixed seed', 'permutation': 'campaign sign-flip exploratory null; independence not assured', 'multiple_testing': 'Holm over ALL 80 candidate trial p-values; blocked/zero-trade p=1', 'chronology': '60/20/20 by signal time with event-end purge and 1 M15-bar embargo; rolling expanding train with 12-trade test blocks', 'calibration': 'fit scaler/model only on train; Platt on validation only; compare Brier to train prevalence', 'small_sample_threshold': 30},
        'failure_and_promotion_policy': {'record_every_trial': True, 'zero_trade_and_all_loss_trials_retained': True, 'no_parameter_threshold_search': True, 'no_champion_change': True, 'new_holdout_required': True, 'future_research_interval': '[2026-11-10,2026-12-10) UTC, currently unavailable', 'minimum_complete_future_campaigns': 30, 'necessary_not_sufficient': ['positive net after base and doubled costs', 'PF > 1 in future holdout', 'positive lower uncertainty bound for expected R', 'acceptable predeclared equity-risk limit and no hidden open trades', 'native parity or explicit independently validated execution engine']},
        'unavailable': ['untouched test data', 'M5/M30 original feed histories', 'PU Prime executable bid/ask and account symbol specification', 'point-in-time macro/news history', 'actual broker executions'],
    }
    target.write_text(json.dumps(registration, indent=2, ensure_ascii=False) + '\n', 'utf-8')
    return {'path': str(target), 'registered_utc': registration['registered_utc'], 'sha256': sha(target), 'trial_count': len(experiments)}


if __name__ == '__main__':
    print(json.dumps(register(), ensure_ascii=False))
