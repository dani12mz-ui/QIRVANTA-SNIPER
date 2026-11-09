"""Offline future-outcome labels; ambiguous OHLC paths are never guessed."""


def triple_barrier(bars, entry_index, side, entry, stop, target, horizon=48):
    if side not in (-1, 1) or horizon < 1 or not 0 <= entry_index < len(bars) or (entry-stop)*side <= 0 or (target-entry)*side <= 0:
        raise ValueError('invalid barrier geometry')
    for i in range(entry_index, min(len(bars), entry_index+horizon)):
        b = bars[i]
        sl = b['low'] <= stop if side == 1 else b['high'] >= stop
        tp = b['high'] >= target if side == 1 else b['low'] <= target
        if sl or tp:
            return {'label': None if sl and tp else int(tp), 'status': 'AMBIGUOUS_SAME_BAR' if sl and tp else 'TARGET' if tp else 'STOP', 'available_at': b['time_close'], 'end_index': i}
    i = min(len(bars)-1, entry_index+horizon-1)
    complete = entry_index+horizon <= len(bars)
    return {'label': int((bars[i]['close']-entry)*side > 0) if complete else None, 'status': 'HORIZON_CLOSE' if complete else 'CENSORED', 'available_at': bars[i]['time_close'], 'end_index': i}
