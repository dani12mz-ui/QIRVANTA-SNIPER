#!/usr/bin/env python3
"""Reproduce one self-contained strategy from modules. NOT a Pine compiler."""
from pathlib import Path
import argparse
import hashlib
import json

ROOT = Path(__file__).resolve().parents[1]
COMMON = ['inputs', 'features', 'types_helpers', 'decision', 'paper_engine']
TAIL = ['alerts_helpers', 'native_adapter', 'display_context', 'visual_alerts', 'alerts']
OUTPUT = 'QIRVANTA_UNIFIED.pine'
OUTPUTS = {OUTPUT: 'native'}

def text(name, root=None):
    return ((root or ROOT) / 'src' / f'{name}.pineinc').read_text(encoding='utf-8')

def assemble(root=None):
    root = root or ROOT
    blocks = ''.join(text(name, root) for name in COMMON)
    digest = hashlib.sha256(blocks.encode('utf-8')).hexdigest()
    common = f'const string CORE_SHA256 = "{digest}"\n// BEGIN CANONICAL CORE {digest}\n{blocks}// END CANONICAL CORE\n'
    return {OUTPUT: text('native_header', root) + common + ''.join(text(name, root) for name in TAIL)}, digest

def build(check=False):
    sources, digest = assemble()
    drift = []
    metadata = {}
    for filename, source in sources.items():
        data = source.encode('utf-8')
        metadata[filename] = {'lines':len(source.splitlines()), 'sha256':hashlib.sha256(data).hexdigest()}
        for directory in (ROOT,):
            path = directory / filename
            if check:
                if not path.exists() or path.read_bytes() != data:
                    drift.append(str(path.relative_to(ROOT.parent)))
            else:
                path.write_bytes(data)
    report = {'canonical_core_sha256':digest, 'outputs':metadata, 'pine_compile':'NOT_TESTED_BY_GENERATOR', 'assembly':['native_header',*COMMON,*TAIL], 'source':'unified/src; corrected baseline in ../refactored and ../audited is immutable', 'line_endings':'LF'}
    if not check:
        for directory in (ROOT,):
            (directory/'reports').mkdir(exist_ok=True)
            (directory/'reports/build.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
    print(json.dumps({'mode':'CHECK' if check else 'BUILD','drift':drift,**report},ensure_ascii=False,indent=2))
    return 1 if drift else 0

if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check',action='store_true',help='Compare without writing any files')
    raise SystemExit(build(parser.parse_args().check))
