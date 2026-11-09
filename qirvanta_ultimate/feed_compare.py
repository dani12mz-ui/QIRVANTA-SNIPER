"""Compare observations by exact UTC bar-open timestamp without merging feeds."""
from math import sqrt
from statistics import mean

from qirvanta_ultimate.risk import millis, number
from research.validation.statistics import quantile


def compare_closed_bars(reference, broker, *, interval_ms):
    """Consume per-feed rows {at_ms, close}. Return aggregates only.

    The caller supplies native interval-matched, completed price series. No
    nearest-neighbor matching, missing-bar repair, or inferred bid/ask history.
    """
    if not millis(interval_ms) or interval_ms == 0:
        raise ValueError('POSITIVE_BAR_INTERVAL_REQUIRED')

    def index(rows):
        result = {}
        for row in rows:
            at_ms, close = row['at_ms'], row['close']
            if (not millis(at_ms) or not number(close) or close <= 0
                    or result and at_ms <= next(reversed(result))):
                raise ValueError('INVALID_OR_NONMONOTONE_BAR')
            result[at_ms] = close
        return result

    a, b = index(reference), index(broker)
    common = sorted(a.keys() & b.keys())
    difference = [b[t]-a[t] for t in common]
    bps = [10_000 * (b[t]-a[t])/a[t] for t in common]
    consecutive = [(t0, t1) for t0, t1 in zip(common, common[1:]) if t1-t0 == interval_ms]
    returns_a = [a[t1]/a[t0]-1 for t0, t1 in consecutive]
    returns_b = [b[t1]/b[t0]-1 for t0, t1 in consecutive]

    def time_gaps(prices):
        times = list(prices)
        return sum(t1-t0 > interval_ms for t0, t1 in zip(times, times[1:]))

    def direction(x):
        return (x > 0) - (x < 0)

    return {'status': 'ALIGNED_AGGREGATE' if common else 'NO_EXACT_ALIGNMENT',
            'interval_ms': interval_ms, 'matched': len(common),
            'first_match_ms': common[0] if common else None,
            'last_match_ms': common[-1] if common else None,
            'reference_only': len(a)-len(common), 'broker_only': len(b)-len(common),
            'reference_gap_count': time_gaps(a), 'broker_gap_count': time_gaps(b),
            'mean_broker_minus_reference': mean(difference) if common else None,
            'abs_deviation_p95_bps': quantile([abs(x) for x in bps], .95),
            'max_abs_deviation_bps': max((abs(x) for x in bps), default=None),
            'matched_consecutive_returns': len(consecutive),
            'reference_return_rms_bps': 10_000*sqrt(mean(x*x for x in returns_a)) if consecutive else None,
            'broker_return_rms_bps': 10_000*sqrt(mean(x*x for x in returns_b)) if consecutive else None,
            'direction_disagreements': sum(direction(x) != direction(y) for x, y in zip(returns_a, returns_b)),
            'basis': 'Broker minus reference bar closes. Direction mismatch is descriptive, not strategy signal mismatch.'}
