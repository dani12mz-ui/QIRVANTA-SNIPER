"""Data-only shared trading interval; no returns participate in selection."""
from research.features.causal import build_features


def interval_from_availability(feature_sets, bar_sets, alignment_ms=1800000):
    if set(feature_sets)!=set(bar_sets) or not feature_sets or alignment_ms<=0:
        raise ValueError('Mismatched timeframe data')
    first={}
    for tf, features in feature_sets.items():
        if len(features)!=len(bar_sets[tf]):
            raise ValueError('Feature/bar length mismatch')
        available=[f['available_at'] for f in features if f['ready'] and f['h1']['bias'] is not None and f['h4']['bias'] is not None]
        if not available:
            return {'status':'BLOCKED_INSUFFICIENT_CLOSED_HTF_WARMUP','timeframe':tf,'start':None,'end':None}
        first[tf]=min(available)
    start=((max(first.values())+alignment_ms-1)//alignment_ms)*alignment_ms
    end=(min(bars[-1]['time_close'] for bars in bar_sets.values())//alignment_ms)*alignment_ms
    if start>=end:
        return {'status':'BLOCKED_NO_SHARED_POST_WARMUP_INTERVAL','first_valid_by_tf':first,'start':start,'end':end}
    return {'status':'AVAILABLE','first_valid_by_tf':first,'start':start,'end':end,'alignment_ms':alignment_ms,
            'criterion':'latest first ready entry feature with completed H1/H4 bias (SMA-seeded EMA20/50), ceil UTC half-hour; min last bar close floor half-hour',
            'uses_returns':False,'partial_end_bars_excluded':{tf:sum(b['time_close']>end for b in bars) for tf,bars in bar_sets.items()}}


def choose_common_window(bar_sets):
    return interval_from_availability({tf:build_features(bars) for tf,bars in bar_sets.items()},bar_sets)
