#!/usr/bin/env python3
"""Package verified sources and baseline; never fabricate acceptance or performance."""
from pathlib import Path
import argparse, hashlib, json, zipfile
from build import build
from provenance import ROOT, source_fingerprint

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args()
    if build(check=True):raise SystemExit('Build drift: regenerate and retest before packaging')
    tests=json.loads((ROOT/'reports/tests.json').read_text(encoding='utf-8'))
    acceptance=json.loads((ROOT/'reports/ACCEPTANCE.json').read_text(encoding='utf-8'))
    digest,_=source_fingerprint()
    if not tests['successful'] or tests['tested_source_sha256']!=digest:raise SystemExit('Tests missing, failed or stale')
    if acceptance['tested_source_sha256']!=digest:raise SystemExit('Acceptance stale')
    entries={}
    for directory in (ROOT,ROOT.parent/'audited'):
        for p in sorted(directory.rglob('*')):
            if not p.is_file():continue
            rel=p.relative_to(ROOT.parent)
            if any(x in rel.parts for x in ('__pycache__','.venv','.git')) or p.suffix=='.pyc':continue
            if p.resolve()==args.out.resolve():continue
            if p.suffix=='.zip' and rel.as_posix()!='refactored/baseline/delivery_103bc307.zip':continue
            if p.name in ('ohlc.csv','pine_events.jsonl','manifest.json') and 'data' in rel.parts:continue
            if 'real_run' in rel.parts or 'paired_run' in rel.parts:continue
            entries[rel.as_posix()]=p.read_bytes()
    entries['SHA256SUMS.txt']=''.join(hashlib.sha256(data).hexdigest()+'  '+name+'\n' for name,data in sorted(entries.items())).encode()
    args.out.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(args.out,'w',zipfile.ZIP_DEFLATED,compresslevel=9) as z:
        for name,data in sorted(entries.items()):
            info=zipfile.ZipInfo('QIRVANTA/'+name,date_time=(2026,11,8,0,0,0));info.compress_type=zipfile.ZIP_DEFLATED
            z.writestr(info,data)
    with zipfile.ZipFile(args.out) as z:
        if z.testzip() is not None:raise SystemExit('ZIP integrity failure')
    print(json.dumps({'zip':str(args.out),'sha256':hashlib.sha256(args.out.read_bytes()).hexdigest(),'files':len(entries),'tested_source_sha256':digest},indent=2))

if __name__=='__main__':main()
