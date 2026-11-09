"""Validate actual M5/M15/M30 Pine Logs; archive bars locally outside the repo."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT=Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))

# Reuse the immutable Decimal-safe generic CSV/bar-block reader, not its M15-only inspector.
from diagnostics.protocol.extract_bars import parse_export,canonical_csv,json_ready,utc,COLUMNS


def _integer(value,name):
    if isinstance(value,bool) or not isinstance(value,int):
        raise ValueError(f'{name} must be an integer JSON token')
    return value


def inspect_platform(parsed,*,expected_timeframe,expected_source_sha256,expected_symbol='OANDA:XAUUSD'):
    timeframe=str(expected_timeframe)
    if timeframe not in ('5','15','30'):
        raise ValueError('Supported actual entry timeframes: 5, 15, 30')
    if not re.fullmatch('[0-9a-f]{64}',expected_source_sha256):
        raise ValueError('Expected exact lowercase SHA-256 of the observed strategy source')
    rows,run,snapshot=parsed['rows'],parsed['run'],parsed['snapshot']
    interval=int(timeframe)*60000
    if run.get('schema')!='qirvanta.native.v1' or run.get('symbol')!=expected_symbol or str(run.get('timeframe'))!=timeframe:
        raise ValueError('Unexpected schema, symbol or timeframe')
    if run.get('source_sha256')!=expected_source_sha256:
        raise ValueError('Observed strategy source hash mismatch')
    for key in ('bar_count','first_bar_time','last_bar_open','cutoff'):
        _integer(run.get(key),key)
    for key in ('at_time','at_bar'):
        _integer(snapshot.get(key),key)
    if run['bar_count']!=len(rows) or snapshot['at_bar']!=len(rows)-1:
        raise ValueError('Export count or snapshot index mismatch')
    if not rows or rows[0][0]!=run['first_bar_time'] or rows[-1][0]!=run['last_bar_open'] or rows[-1][1]!=run['cutoff'] or snapshot['at_time']!=run['cutoff']:
        raise ValueError('First/last/cutoff bounds mismatch')
    seen=set();gaps=[]
    for i,row in enumerate(rows):
        opening,closing=row[:2]
        _integer(opening,'bar time');_integer(closing,'bar time_close')
        if closing-opening!=interval or opening%interval or closing%interval:
            raise ValueError(f'Wrong duration or UTC alignment at row {i}')
        if opening in seen:
            raise ValueError(f'Duplicate timestamp at row {i}')
        seen.add(opening)
        if min(row[2:6])<=0:
            raise ValueError('Nonpositive OHLC')
        if closing>run['cutoff']:
            raise ValueError('Bar beyond cutoff')
        if i:
            previous_close=rows[i-1][1]
            if opening<previous_close:
                raise ValueError(f'Backwards or overlapping interval at row {i}')
            if opening>previous_close:
                gaps.append({'previous_row':i-1,'next_row':i,'previous_close':previous_close,'next_open':opening,'unobserved_intervals':(opening-previous_close)//interval})
    return {'status':'PASS_ACTUAL_SERIALIZED_PLATFORM_EXPORT','symbol':expected_symbol,'timeframe':timeframe,'bar_count':len(rows),'interval_ms':interval,'first_open':rows[0][0],'first_open_utc':utc(rows[0][0]),'last_close':rows[-1][1],'last_close_utc':utc(rows[-1][1]),'gaps':gaps,'gap_count':len(gaps),'null_counts':{c:sum(r[j] is None for r in rows) for j,c in enumerate(COLUMNS)},'checks':{'source_hash':True,'schema_symbol_timeframe':True,'native_run_snapshot_identity':True,'count_index_bounds':True,'strict_integer_times':True,'uniform_interval_utc_alignment':True,'no_duplicates_backwards_overlaps':True,'no_future_rows':True}}


def extract_platform(source,destination,*,expected_timeframe,expected_source_sha256,expected_log_sha256=None,expected_symbol='OANDA:XAUUSD'):
    source,destination=Path(source).resolve(),Path(destination).resolve()
    if destination.exists():
        raise FileExistsError('Refusing to overwrite an existing local dataset')
    if destination.is_relative_to(ROOT):
        raise ValueError('New full market data must remain outside the repository; redistribution rights are not established')
    raw=source.read_bytes();raw_sha=hashlib.sha256(raw).hexdigest()
    if expected_log_sha256 is not None and raw_sha!=expected_log_sha256:
        raise ValueError('Downloaded log SHA-256 mismatch')
    parsed=parse_export(raw)
    report=inspect_platform(parsed,expected_timeframe=expected_timeframe,expected_source_sha256=expected_source_sha256,expected_symbol=expected_symbol)
    payload=canonical_csv(parsed['rows'])
    manifest={'schema':'qirvanta.research.local-platform-manifest.v1','created_utc':datetime.now(timezone.utc).isoformat(),'source_log':str(source),'source_log_sha256':raw_sha,'source_log_bytes':len(raw),'expected_strategy_source_sha256':expected_source_sha256,'source_metadata':parsed['metadata'],'run_metadata':parsed['run'],'cutoff_snapshot':parsed['snapshot'],'canonical_csv':{'file':'bars.csv','sha256':hashlib.sha256(payload).hexdigest(),'bytes':len(payload)},'redistribution':'LOCAL_ONLY_RIGHTS_NOT_ESTABLISHED_DO_NOT_COMMIT_RAW_OR_FULL_OHLC','volume_status':'PROVIDER_VOLUME_SEMANTICS_UNVERIFIED_NOT_TRUE_VWAP','history_status':'ACTUAL_PLATFORM_EXPORT_ALREADY_OBSERVED_CALENDAR_PERIOD_NOT_UNTOUCHED','precision':'Exported Decimal serialization preserved; not proof of backend full precision','no_reconstruction':'All rows and order preserved; no gap fill, deduplication or synthetic candles','timestamp_scope':'CSV log Date is logging-bar metadata; market timestamps are the explicit bar columns','native_accounting':'Separate native journal reconciliation required; extracting OHLC does not validate native P&L',**report}
    manifest=json_ready(manifest)
    destination.mkdir(parents=True,exist_ok=False)
    (destination/'bars.csv').write_bytes(payload)
    (destination/'dataset_manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    return manifest


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('source',type=Path);p.add_argument('destination',type=Path)
    p.add_argument('--timeframe',choices=('5','15','30'),required=True)
    p.add_argument('--expected-source-sha256',required=True)
    p.add_argument('--expected-log-sha256')
    a=p.parse_args()
    result=extract_platform(a.source,a.destination,expected_timeframe=a.timeframe,expected_source_sha256=a.expected_source_sha256,expected_log_sha256=a.expected_log_sha256)
    print(json.dumps({k:result[k] for k in ('status','bar_count','timeframe','first_open_utc','last_close_utc','source_log_sha256','canonical_csv')},ensure_ascii=False))


if __name__=='__main__':main()
