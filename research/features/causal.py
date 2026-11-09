"""Single-pass, closed-bar research features. No Pine equivalence is implied."""
from collections import deque
from datetime import datetime, timezone
from math import isfinite, sqrt
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from research.data.loader import validate_bars


class Smooth:
    def __init__(self, length, wilder=False):
        self.length, self.alpha = length, (1 / length if wilder else 2 / (length + 1))
        self.seed, self.value = [], None

    def add(self, value):
        if value is None:
            return None
        if self.value is None:
            self.seed.append(value)
            if len(self.seed) == self.length:
                self.value = sum(self.seed) / self.length
        else:
            self.value += self.alpha * (value - self.value)
        return self.value


class ClosedContext:
    """UTC-aligned completed H1/H4 bins; reject incomplete/gapped bins."""
    def __init__(self, minutes):
        self.period = minutes * 60000
        self.bucket = self.first = self.last = None
        self.close = self.available_at = None
        self.complete = True
        self.fast, self.slow = Smooth(20), Smooth(50)
        self.value = {'bias': None, 'available_at': None, 'close': None}

    def add(self, b):
        bucket = int(b['time']) // self.period
        if bucket != self.bucket:
            self.bucket, self.first, self.last = bucket, b['time'], b['time']
            self.complete = b['time'] % self.period == 0
        if b['time'] != self.last:
            self.complete = False
        self.last = b['time_close']
        if b['time_close'] == (bucket + 1) * self.period:
            if self.complete:
                fast, slow = self.fast.add(b['close']), self.slow.add(b['close'])
                bias = None if slow is None else (1 if b['close'] > fast > slow else -1 if b['close'] < fast < slow else 0)
                self.value = {'bias': bias, 'available_at': b['time_close'], 'close': b['close']}
            else:
                self.value = {'bias': None, 'available_at': b['time_close'], 'close': None}
        return dict(self.value)


def _zones():
    try:
        return {'LONDON': ZoneInfo('Europe/London'), 'NEW_YORK': ZoneInfo('America/New_York')}
    except ZoneInfoNotFoundError:
        return {}


def build_features(bars, *, volume_kind='UNVERIFIED', verified_bar_vwap=False):
    validate_bars(bars)
    ema20, ema50, ema200 = Smooth(20), Smooth(50), Smooth(200)
    atr_s, plus_s, minus_s, adx_s, gain_s, loss_s = [Smooth(14, True) for _ in range(6)]
    atr_history, changes = deque(maxlen=40), deque(maxlen=20)
    contexts = {'h1': ClosedContext(60), 'h4': ClosedContext(240)}
    zones, opening = _zones(), {}
    out = []
    pivot_high = pivot_low = previous_pivot_high = previous_pivot_low = None
    pivot_high_at = pivot_low_at = None
    current_day, current_week = None, None
    day_hi = day_lo = week_hi = week_lo = None
    prior_day_high = prior_day_low = prior_week_high = prior_week_low = None
    v_sum = pv_sum = p2v_sum = 0.0
    vwap_day_valid = True
    for i, b in enumerate(bars):
        o, h, l, c = (b[k] for k in ('open', 'high', 'low', 'close'))
        prev = bars[i-1] if i else None
        gap = bool(prev and b['time'] != prev['time_close'])
        dt = datetime.fromtimestamp(b['time']/1000, timezone.utc)
        day, week = dt.date().isoformat(), (dt.isocalendar().year, dt.isocalendar().week)
        if day != current_day:
            prior_day_high, prior_day_low = (day_hi, day_lo) if current_day is not None else (None, None)
            day_hi, day_lo, current_day = h, l, day
            v_sum = pv_sum = p2v_sum = 0.0
            vwap_day_valid = b['time'] % 86400000 == 0
        else:
            day_hi, day_lo = max(day_hi, h), min(day_lo, l)
        if week != current_week:
            prior_week_high, prior_week_low = (week_hi, week_lo) if current_week is not None else (None, None)
            week_hi, week_lo, current_week = h, l, week
        else:
            week_hi, week_lo = max(week_hi, h), min(week_lo, l)
        tr = max(h-l, abs(h-prev['close']), abs(l-prev['close'])) if prev else h-l
        atr = atr_s.add(tr)
        if atr is not None:
            atr_history.append(atr)
        atr_mean = sum(atr_history)/40 if len(atr_history)==40 else None
        up = h-prev['high'] if prev else 0
        down = prev['low']-l if prev else 0
        plus, minus = plus_s.add(max(up, 0) if up>down else 0), minus_s.add(max(down,0) if down>up else 0)
        dip = 100*plus/atr if atr and plus is not None else None
        dim = 100*minus/atr if atr and minus is not None else None
        dx = (100*abs(dip-dim)/(dip+dim) if dip+dim else 0) if dip is not None and dim is not None else None
        adx = adx_s.add(dx)
        change = c-prev['close'] if prev else None
        gain, loss = gain_s.add(max(change,0) if change is not None else None), loss_s.add(max(-change,0) if change is not None else None)
        rsi = None if gain is None or loss is None else (100*gain/(gain+loss) if gain+loss else 50)
        if change is not None:
            changes.append(abs(change))
        efficiency = (abs(c-bars[i-20]['close'])/sum(changes) if sum(changes) else 0) if i>=20 else None
        e20,e50,e200 = ema20.add(c),ema50.add(c),ema200.add(c)
        prior = bars[max(0,i-20):i]
        short = bars[max(0,i-3):i]
        range20_high = max(x['high'] for x in prior) if len(prior)==20 else None
        range20_low = min(x['low'] for x in prior) if len(prior)==20 else None
        new_pivot_high = new_pivot_low = None
        if i>=6 and not any(bars[j]['time'] != bars[j-1]['time_close'] for j in range(i-5,i+1)):
            center = bars[i-3]
            others = bars[i-6:i-3]+bars[i-2:i+1]
            if all(center['high']>x['high'] for x in others):
                previous_pivot_high, pivot_high = pivot_high, center['high']
                pivot_high_at, new_pivot_high = i, pivot_high
            if all(center['low']<x['low'] for x in others):
                previous_pivot_low, pivot_low = pivot_low, center['low']
                pivot_low_at, new_pivot_low = i, pivot_low
        ratio = atr/atr_mean if atr and atr_mean else None
        ready = e200 is not None and adx is not None and efficiency is not None and atr_mean is not None and atr>0
        direction = 1 if ready and c>e20>e50>e200 else -1 if ready and c<e20<e50<e200 else 0
        regime = 'UNSUPPORTED'
        if ready:
            regime = ('COMPRESSION' if ratio<.75 else 'EXPANSION' if ratio>1.5 else 'UPTREND' if direction==1 and adx>=20 and efficiency>=.25 else 'DOWNTREND' if direction==-1 and adx>=20 and efficiency>=.25 else 'RANGE' if adx<20 and efficiency<.25 else 'TRANSITION')
        session_ranges = {}
        sessions=[]
        for name,z in zones.items():
            local = dt.astimezone(z)
            minute = local.hour*60+local.minute
            start = 480 if name=='LONDON' else 570
            if local.weekday()<5 and start<=minute<start+480:
                sessions.append(name)
            key=(name,local.date().isoformat())
            if start<=minute<start+30:
                if key not in opening:
                    opening[key]={'high':h,'low':l,'valid':minute==start,'last':b['time_close']}
                else:
                    state=opening[key]
                    state['valid'] &= state['last']==b['time']
                    state.update(high=max(state['high'],h),low=min(state['low'],l),last=b['time_close'])
                # Do not release a range until its last bar has actually closed.
                opening[key]['complete'] = minute+(b['time_close']-b['time'])/60000==start+30
            state=opening.get(key)
            if state and state.get('valid') and state.get('complete') and start+30<=minute<start+150 and local.weekday()<5:
                session_ranges[name]={'high':state['high'],'low':state['low'],'date':key[1]}
        volume, bar_vwap = b.get('volume'), b.get('bar_vwap')
        valid_volume = volume_kind=='EXCHANGE_TRADED' and verified_bar_vwap and volume is not None and isfinite(volume) and volume>0 and bar_vwap is not None and isfinite(bar_vwap) and l<=bar_vwap<=h
        vwap_day_valid &= valid_volume and not gap
        if valid_volume:
            v_sum += volume
            pv_sum += bar_vwap*volume
            p2v_sum += bar_vwap*bar_vwap*volume
        vwap = pv_sum/v_sum if v_sum and vwap_day_valid else None
        vwap_sd = sqrt(max(0,p2v_sum/v_sum-vwap*vwap)) if vwap is not None else None
        f={'index':i,'time':b['time'],'available_at':b['time_close'],'ready':ready,'gap':gap,'atr':atr,'atr_mean':atr_mean,'atr_ratio':ratio,'adx':adx,'di_plus':dip,'di_minus':dim,'rsi':rsi,'ema20':e20,'ema50':e50,'ema200':e200,'efficiency':efficiency,'regime':regime,'direction':direction,'body':abs(c-o),'range':h-l,'close_location':(c-l)/(h-l) if h>l else .5,'upper_wick':h-max(o,c),'lower_wick':min(o,c)-l,'range20_high':range20_high,'range20_low':range20_low,'range3_high':max(x['high'] for x in short) if len(short)==3 else None,'range3_low':min(x['low'] for x in short) if len(short)==3 else None,'pivot_high':pivot_high,'pivot_low':pivot_low,'pivot_high_confirmed_index':pivot_high_at,'pivot_low_confirmed_index':pivot_low_at,'new_pivot_high':new_pivot_high,'new_pivot_low':new_pivot_low,'equal_highs':previous_pivot_high is not None and atr is not None and abs(pivot_high-previous_pivot_high)<=.12*atr,'equal_lows':previous_pivot_low is not None and atr is not None and abs(pivot_low-previous_pivot_low)<=.12*atr,'fvg_up':i>=2 and l>bars[i-2]['high'],'fvg_down':i>=2 and h<bars[i-2]['low'],'prior_utc_day_high':prior_day_high,'prior_utc_day_low':prior_day_low,'prior_utc_week_high':prior_week_high,'prior_utc_week_low':prior_week_low,'sessions':sessions,'session_timezone_available':bool(zones),'opening_ranges':session_ranges,'vwap':vwap,'vwap_sd':vwap_sd,'vwap_available':vwap is not None,'volume_kind':volume_kind}
        for name,context in contexts.items():
            f[name]=context.add(b)
        out.append(f)
    return out
