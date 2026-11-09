"""Lossless OHLC aggregation of complete M15 pairs; never synthesize M5 data."""
from collections import defaultdict
import hashlib
import json
from math import isfinite

from research.data.loader import validate_bars


def canonical_sha256(rows):
    return hashlib.sha256(json.dumps(rows,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()).hexdigest()


def aggregate_m15_to_m30(bars):
    if validate_bars(bars)!=900000:
        raise ValueError('Exact M15 input required for M30 paired aggregation')
    groups=defaultdict(list)
    for i,bar in enumerate(bars):
        groups[int(bar['time'])//1800000].append((i,bar))
    output,dropped,provenance=[],[],[]
    for bucket,items in sorted(groups.items()):
        start=bucket*1800000
        indices=[i for i,b in items]
        if len(items)!=2 or items[0][1]['time']!=start or items[0][1]['time_close']!=start+900000 or items[1][1]['time']!=start+900000 or items[1][1]['time_close']!=start+1800000:
            dropped.append({'bucket_open':start,'source_indices':indices,'reason':'INCOMPLETE_OR_NONCONTIGUOUS_UTC_PAIR'})
            continue
        first,second=items[0][1],items[1][1]
        volumes=[b.get('volume') for _,b in items]
        volume=sum(volumes) if all(isinstance(v,(int,float)) and isfinite(v) and v>=0 for v in volumes) else None
        output.append({'time':start,'time_close':start+1800000,'open':first['open'],'high':max(first['high'],second['high']),'low':min(first['low'],second['low']),'close':second['close'],'volume':volume})
        provenance.append(indices)
    validate_bars(output)
    manifest={'schema':'qirvanta.research.derived-timeframe.v1','method':'UTC_ALIGNED_EXACT_TWO_M15_BARS','source_bars':len(bars),'source_canonical_sha256':canonical_sha256(bars),'derived_bars':len(output),'derived_canonical_sha256':canonical_sha256(output),'source_index_pairs':provenance,'dropped_partial_bins':dropped,'dropped_source_bars':sum(len(d['source_indices']) for d in dropped),'first_open':output[0]['time'] if output else None,'last_close':output[-1]['time_close'] if output else None,'volume_status':'SUM_OF_UNVERIFIED_PROVIDER_VOLUME_NOT_TRUE_VWAP','history_status':'SAME_ALREADY_OBSERVED_HISTORY_NOT_NEW_OOS','native_M30_parity':False,'raw_copies_written':False,'limitations':['Bar-based feature periods, cooldown and hold horizons have doubled clock duration at unchanged parameters','Next-open latency and same-bar ambiguity differ with coarser bars','Incomplete pairs are omitted and explicitly logged; no fabricated prices','HTF feature context is recomputed causally; source M15 HTF columns are not copied','Cannot infer original M5 bars or intrabar price path from M15 OHLC']}
    return output,manifest
