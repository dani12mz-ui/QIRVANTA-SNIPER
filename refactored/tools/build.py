#!/usr/bin/env python3
"""Assemble the recovered audited sources deterministically. NOT a Pine compiler."""
from pathlib import Path
import argparse
import hashlib
import json

ROOT = Path(__file__).resolve().parents[1]
COMMON = ['inputs', 'features', 'types_helpers', 'decision', 'paper_engine']
OUTPUTS = {'QIRVANTA_SNIPER_PRO.pine': 'indicator', 'QIRVANTA_SNIPER_VALIDATION.pine': 'native'}

def text(name, root=ROOT):
    return (root / 'src' / f'{name}.pineinc').read_text(encoding='utf-8')

def assemble(root=ROOT):
    blocks = ''.join(text(name, root) for name in COMMON)
    digest = hashlib.sha256(blocks.encode('utf-8')).hexdigest()
    common = f'const string CORE_SHA256 = "{digest}"\n// BEGIN CANONICAL CORE {digest}\n{blocks}// END CANONICAL CORE\n'
    return {filename: text(kind+'_header', root) + common + text(kind+'_adapter', root) + text('visual_alerts', root) + text(kind+'_extra', root) for filename, kind in OUTPUTS.items()}, digest

def build(check=False):
    sources, digest = assemble()
    drift = []
    metadata = {}
    for filename, source in sources.items():
        data = source.encode('utf-8')
        metadata[filename] = {'lines':len(source.splitlines()), 'sha256':hashlib.sha256(data).hexdigest()}
        for directory in (ROOT, ROOT.parent / 'audited'):
            path = directory / filename
            if check:
                if not path.exists() or path.read_bytes() != data:
                    drift.append(str(path.relative_to(ROOT.parent)))
            else:
                path.write_bytes(data)
    report = {'canonical_core_sha256':digest, 'outputs':metadata, 'pine_compile':'UNVERIFIED', 'source':'refactored/src recovered from audited at 103bc307; see baseline manifest', 'line_endings':'LF'}
    if not check:
        for directory in (ROOT, ROOT.parent / 'audited'):
            (directory/'reports').mkdir(exist_ok=True)
            (directory/'reports/build.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
    print(json.dumps({'mode':'CHECK' if check else 'BUILD','drift':drift,**report},ensure_ascii=False,indent=2))
    return 1 if drift else 0

if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check',action='store_true',help='Compare without writing any files')
    raise SystemExit(build(parser.parse_args().check))
