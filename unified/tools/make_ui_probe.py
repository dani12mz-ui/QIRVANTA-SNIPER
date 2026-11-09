"""Attach the preserved read-only native observer to the current UI-only build.

Creates a new temporary .txt, never overwrites the production Pine or evidence.
Observation cutoff: 2026-11-09T02:45Z on M5/M15, 02:30Z on M30.
No order rule is modified.
"""
from pathlib import Path
import argparse
import hashlib
import json
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
BASELINE = '48e5cddab17ea831bdc277f17f0406329fdfe39c'
BASE_SHA = 'c6bfe1af0604c447eddaed1db5195bfe2c07d3826e7fafc70113235e9f25f1a5'
OBSERVER_SHA = '430352649bbb906f6723c947a7f00f1e5213715d7eac5724ef47d883c0529aa9'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output',type=Path)
    parser.add_argument('--timeframe-minutes',type=int,choices=(5,15,30),default=15)
    args=parser.parse_args()
    output = args.output.resolve()
    manifest = output.with_suffix('.manifest.json')
    if output.suffix.lower() != '.txt' or any(p.exists() or p.is_symlink() for p in (output, manifest)):
        raise ValueError('Use new .txt and manifest paths; existing outputs cannot be overwritten')
    base = subprocess.check_output(['git','show',f'{BASELINE}:unified/QIRVANTA_UNIFIED.pine'],cwd=ROOT)
    if digest(base) != BASE_SHA:
        raise ValueError('Wrong immutable baseline')
    current = (ROOT/'unified/QIRVANTA_UNIFIED.pine').read_bytes()
    with tempfile.TemporaryDirectory() as temporary:
        probe = Path(temporary)/'baseline.txt'
        subprocess.run([sys.executable,'-B',str(ROOT/'diagnostics/make_probe.py'),str(probe)],cwd=ROOT,check=True,capture_output=True)
        original = probe.read_bytes()
    if not original.startswith(base) or digest(original) != OBSERVER_SHA:
        raise ValueError('Preserved observer changed')
    appendix = original[len(base):].decode('utf-8')
    if appendix.count(BASE_SHA) != 1 or appendix.count('"baseline|" + runModel') != 1:
        raise ValueError('Unexpected observer metadata anchors')
    appendix = appendix.replace(BASE_SHA,digest(current)).replace('"baseline|" + runModel','"ui_only|" + runModel')
    appendix = appendix.replace('// DIAGNOSTIC OBSERVER ONLY. Baseline above is byte-identical to commit 48e5cdd.', '// READ-ONLY UI PARITY OBSERVER. Current presentation build is above; trading parity must be checked independently.')
    # M30 cannot close at :45. This changes only observation/export timing.
    cutoff='02:30' if args.timeframe_minutes==30 else '02:45'
    appendix=appendix.replace('09 Nov 2026 02:45 +0000',f'09 Nov 2026 {cutoff} +0000')
    appendix=appendix.replace('Pine observed OANDA:XAUUSD M15','Pine observed OHLC/HTF; symbol and timeframe in run header')
    appendix += f'\nif barstate.isfirst and timeframe.in_seconds() != {args.timeframe_minutes*60}\n    runtime.error("Observer timeframe does not match the registered export")\n'
    result = current + appendix.encode('utf-8')
    output.parent.mkdir(parents=True,exist_ok=True)
    with output.open('xb') as stream:
        stream.write(result)
    record = {'source_sha256':digest(current),'editor_buffer_sha256':digest(result),'baseline_commit':BASELINE,'preserved_observer_buffer_sha256':OBSERVER_SHA,'timeframe_minutes':args.timeframe_minutes,'cutoff_utc':f'2026-11-09T{cutoff}:00Z','observer_changes':['source identity','run label','descriptive comment','exact timeframe guard','aligned observation cutoff only'],'financial_changes':False,'runtime_test':'NOT_PERFORMED_BY_GENERATOR','source':str(ROOT/'unified/QIRVANTA_UNIFIED.pine'),'output':str(output)}
    with manifest.open('x',encoding='utf-8',newline='\n') as stream:
        stream.write(json.dumps(record,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(record,ensure_ascii=True))


if __name__ == '__main__':
    main()
