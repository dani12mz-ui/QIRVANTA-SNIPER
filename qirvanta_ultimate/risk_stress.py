"""Aggregate stress evidence from the frozen native campaign journal."""
import argparse
from hashlib import sha256
import json
from pathlib import Path
from random import Random

from qirvanta_ultimate.risk import number
from research.validation.statistics import cash_metrics, quantile


SCENARIOS = (
    ('OBSERVED', 1., 0., 0., 0., 0.),
    ('FEE_SLIP_SPREAD', 1.5, .002, .05, 0., 0.),
    ('FINANCING_AND_GAP', 1.5, .002, .05, .15, .5),
    ('LOSS_TAIL', 1., 0., 0., 0., 1.),
)
BASELINE_AUDIT_SHA256 = '05e98c9831308bcbde40aaaa79b96db368dc856d06d4030a113eab00a00caa67'


def drawdown_duration(closures):
    """Milliseconds since the preceding campaign-close high, including open DD."""
    if not closures:
        return 0
    equity = peak = 0.
    peak_at = closures[0][0]
    longest = 0
    underwater = False
    for at_ms, cash in closures:
        equity += cash
        if equity < peak or underwater:
            longest = max(longest, at_ms-peak_at)
        underwater = equity < peak
        if not underwater:
            peak, peak_at = equity, at_ms
    return longest


def joint_adverse_loss(exposures):
    """All supplied exposures gap together. Each tuple is risk, multiplier, cost."""
    result = 0.
    for risk_cash, gap_multiplier, cost_cash in exposures:
        if (not all(number(x) for x in (risk_cash, gap_multiplier, cost_cash))
                or risk_cash < 0 or gap_multiplier < 1 or cost_cash < 0):
            raise ValueError('INVALID_JOINT_STRESS_INPUT')
        result += risk_cash*gap_multiplier+cost_cash
    if not number(result):
        raise ValueError('INVALID_JOINT_STRESS_OUTPUT')
    return result


def order_stress(cash, *, repetitions, seed, initial_cash, floor_cash):
    if (type(repetitions) is not int or not 1 <= repetitions <= 50_000 or not cash
            or not all(number(x) for x in cash)
            or not number(initial_cash) or not number(floor_cash)
            or not 0 <= floor_cash < initial_cash):
        raise ValueError('INVALID_ORDER_STRESS_INPUT')
    rng = Random(seed)
    dips, streaks, crossings = [], [], 0
    for _ in range(repetitions):
        sequence = list(cash)
        rng.shuffle(sequence)
        equity = peak = low = initial_cash
        max_dd = max_streak = streak = 0
        for net in sequence:
            equity += net
            peak, low = max(peak, equity), min(low, equity)
            max_dd = max(max_dd, peak-equity)
            streak = streak+1 if net < 0 else 0
            max_streak = max(max_streak, streak)
        dips.append(max_dd)
        streaks.append(max_streak)
        crossings += low <= floor_cash
    return {'repetitions': repetitions, 'seed': seed, 'initial_cash': initial_cash,
            'declared_capital_floor_cash': floor_cash, 'floor_crossings': crossings,
            'drawdown_cash_p95': quantile(dips, .95), 'losing_streak_p95': quantile(streaks, .95),
            'interpretation': 'Permutation paths conditioned on these outcomes and scenario costs; no estimated live ruin probability.'}


def study(source, *, repetitions=2000, seed=20261109):
    raw = Path(source).read_bytes()
    source_hash = sha256(raw).hexdigest()
    if source_hash != BASELINE_AUDIT_SHA256:
        raise ValueError('NATIVE_BASELINE_AUDIT_HASH_MISMATCH')
    audit = json.loads(raw)
    rows = sorted(audit['campaigns'], key=lambda c: (c['last_exit_time'], c['plan_id']))
    if (audit['status'] != 'PASS_OBSERVED_JOURNAL' or not rows
            or len({r['plan_id'] for r in rows}) != len(rows)
            or any(r['state'] != 'COMPLETE' or r['open_quantity'] != 0 for r in rows)):
        raise ValueError('COMPLETE_VERIFIED_CAMPAIGNS_REQUIRED')
    scenarios = {}
    for name, fee_mult, slip, spread, finance, loss_tail in SCENARIOS:
        closures = []
        for row in rows:
            net = row['realized_net_cash']
            qty = row['closed_quantity']
            # Cash on raw quantity/pointvalue=1. These add only hypothetical costs
            # above the costs already embedded in the recorded TradingView fills.
            elapsed_days = (row['last_exit_time']-row['entry_time'])/86_400_000
            penalty = (row['realized_commission_cash']*(fee_mult-1)
                       + qty*(2*slip+spread+finance*elapsed_days)
                       + loss_tail*max(0, -net))
            closures.append((row['last_exit_time'], net-penalty))
        cash = [amount for _, amount in closures]
        metrics = cash_metrics(cash)
        wins, losses = [x for x in cash if x > 0], [x for x in cash if x < 0]
        metrics['average_win_cash'] = sum(wins)/len(wins) if wins else None
        metrics['average_loss_cash'] = sum(losses)/len(losses) if losses else None
        metrics['mean_net_r'] = sum(x/r['initial_risk_cash'] for x, r in zip(cash, rows))/len(rows)
        metrics['campaign_close_drawdown_duration_hours'] = drawdown_duration(closures)/3_600_000
        scenarios[name] = {'assumptions': {'commission_multiplier': fee_mult,
                            'additional_slippage_per_raw_unit_each_way': slip,
                            'additional_round_trip_spread_per_raw_unit': spread,
                            'financing_per_raw_unit_per_elapsed_day': finance,
                            'extra_observed_loss_fraction': loss_tail}, 'metrics': metrics,
                           'order_stress': order_stress(cash, repetitions=repetitions, seed=seed,
                                                        initial_cash=100_000, floor_cash=99_000)}
    return {'schema': 'qirvanta.ultimate.risk_stress.v1',
            'source_commit': 'c2212d7b7e0b4b7ea4be64c1fb3c9f7b33fa0f71',
            'source_sha256': source_hash, 'symbol': 'OANDA:XAUUSD', 'minutes': 15,
            'interval_utc': ['2026-08-02T22:00:00Z', '2026-11-09T02:45:00Z'],
            'cost_scope': 'Native simulated fills already include 2 ticks and 0.175 cash per raw unit per fill. All rows retained.',
            'campaigns': len(rows), 'native_partials': sum(r['closed_record_count'] for r in rows),
            'native_equity_drawdown_cash': audit['native_equity_max_drawdown'],
            'scenarios': scenarios,
            'coincident_adverse_exposure_illustration': {'cash_loss': joint_adverse_loss(((50, 2, 4), (80, 3, 9))),
                 'assumptions': 'Illustrative two exposures 50 and 80 account-cash risk, gap multipliers 2 and 3, extra costs 4 and 9. No BTC observations or measured correlation.'},
            'limitation': 'Reorders or perturbs completed native campaigns. No broker spread, swap, margin, intra-campaign equity or forward execution data.'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('audit', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    result = json.dumps(study(args.audit), indent=2, ensure_ascii=False, allow_nan=False)
    with args.output.open('x', encoding='utf-8', newline='\n') as stream:
        stream.write(result + '\n')
