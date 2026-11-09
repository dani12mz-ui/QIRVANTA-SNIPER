"""Small transparent estimators; intervals are descriptive, not proof of edge."""
import math
import random
from statistics import mean


def quantile(values, p):
    if not values:
        return None
    x = sorted(values)
    k = (len(x) - 1) * p
    lo = int(k)
    return x[lo] + (x[min(lo + 1, len(x) - 1)] - x[lo]) * (k - lo)


def wilson(wins, n, z=1.959963984540054):
    if n <= 0:
        return [None, None]
    if not 0 <= wins <= n:
        raise ValueError('wins outside sample')
    p = wins / n
    d = 1 + z*z/n
    c = (p + z*z/(2*n)) / d
    h = z * math.sqrt(p*(1-p)/n + z*z/(4*n*n)) / d
    return [max(0, c-h), min(1, c+h)]


def block_bootstrap(values, repetitions=2000, block=5, seed=20261109):
    if not values:
        return {'n': 0, 'mean': None, 'ci95': [None, None]}
    if block < 1 or repetitions < 2:
        raise ValueError('invalid bootstrap design')
    if len(values) <= block:
        return {'n': len(values), 'mean': mean(values), 'ci95': [None, None],
                'status': 'INSUFFICIENT_BLOCKS', 'block': block,
                'repetitions': repetitions, 'seed': seed,
                'reason': 'A whole-sample circular block produces a degenerate mean; no inferential interval reported'}
    rng = random.Random(seed)
    n = len(values)
    means = []
    for _ in range(repetitions):
        sample = []
        while len(sample) < n:
            start = rng.randrange(n)
            sample.extend(values[(start+j) % n] for j in range(min(block, n)))
        means.append(mean(sample[:n]))
    return {'n': n, 'mean': mean(values), 'ci95': [quantile(means, .025), quantile(means, .975)], 'block': block, 'repetitions': repetitions, 'seed': seed, 'assumption': 'approximately stationary ordered campaign blocks; small sample and regime changes remain limitations'}


def sign_flip_p(values, repetitions=2000, seed=20261109):
    """One-sided mean>0 sign-flip null; NOT causal or robust to dependent trades."""
    if not values:
        return 1.0
    rng = random.Random(seed)
    observed = sum(values)
    return (1 + sum(sum(x * (1 if rng.randrange(2) else -1) for x in values) >= observed for _ in range(repetitions))) / (repetitions + 1)


def holm(pvalues):
    if any(not 0 <= p <= 1 for p in pvalues):
        raise ValueError('invalid p-value')
    order = sorted(range(len(pvalues)), key=pvalues.__getitem__)
    result = [1.0] * len(pvalues)
    previous = 0
    for rank, i in enumerate(order):
        previous = min(1, max(previous, pvalues[i] * (len(order)-rank)))
        result[i] = previous
    return result


def cash_metrics(values):
    wins = [x for x in values if x > 0]
    losses = [x for x in values if x < 0]
    equity = peak = dd = 0.0
    longest = streak = 0
    for x in values:
        equity += x
        peak = max(peak, equity)
        dd = max(dd, peak-equity)
        streak = streak + 1 if x < 0 else 0
        longest = max(longest, streak)
    return {'n': len(values), 'wins': len(wins), 'losses': len(losses), 'breakeven': len(values)-len(wins)-len(losses), 'net_cash': sum(values), 'mean_cash': mean(values) if values else None, 'profit_factor': sum(wins)/-sum(losses) if losses else None, 'profit_factor_status': 'DEFINED' if losses else 'NO_LOSSES' if values else 'NO_TRADES', 'win_rate': len(wins)/len(values) if values else None, 'win_rate_wilson95': wilson(len(wins), len(values)), 'campaign_close_drawdown': dd, 'max_consecutive_losses': longest, 'p05_cash': quantile(values, .05), 'small_n': len(values) < 30}


def sequence_stress(values, repetitions=2000, seed=20261109):
    """Permutation changes order only, never total P&L; no future-return claim."""
    rng = random.Random(seed)
    dd, streaks = [], []
    for _ in range(repetitions):
        sample = list(values)
        rng.shuffle(sample)
        m = cash_metrics(sample)
        dd.append(m['campaign_close_drawdown'])
        streaks.append(m['max_consecutive_losses'])
    return {'method': 'permutation of complete campaign cash outcomes, not native equity simulation', 'seed': seed, 'repetitions': repetitions, 'dd_p50': quantile(dd, .5), 'dd_p95': quantile(dd, .95), 'losing_streak_p95': quantile(streaks, .95)}


def chronological_split(events, train_fraction=.6, validation_fraction=.2, embargo_ms=900000):
    """Events must have start/end UTC millis; purge labels reaching later features."""
    if not 0 < train_fraction < 1 or not 0 < validation_fraction < 1-train_fraction or embargo_ms < 0:
        raise ValueError('invalid split')
    ordered = sorted(events, key=lambda e: (e['start'], e['end']))
    if any(e['end'] < e['start'] for e in ordered):
        raise ValueError('event ends before availability')
    n = len(ordered)
    a, b = int(n*train_fraction), int(n*(train_fraction+validation_fraction))
    first, second, third = ordered[:a], ordered[a:b], ordered[b:]
    train_boundary = second[0]['start'] if second else math.inf
    validation_boundary = third[0]['start'] if third else math.inf
    train = [e for e in first if e['end'] < train_boundary-embargo_ms]
    validation = [e for e in second if e['end'] < validation_boundary-embargo_ms]
    return {'train': train, 'validation': validation, 'test': third, 'purged': [e for e in first if e not in train] + [e for e in second if e not in validation], 'embargo_ms': embargo_ms}


def walk_forward(events, initial_train=24, test_size=12, embargo_ms=900000):
    ordered = sorted(events, key=lambda e: (e['start'], e['end']))
    folds = []
    for start in range(initial_train, len(ordered), test_size):
        test = ordered[start:start+test_size]
        train = [e for e in ordered[:start] if e['end'] < test[0]['start']-embargo_ms]
        folds.append({'train': train, 'test': test, 'purged_n': start-len(train)})
    return folds
