"""Record only the new accounting parser tests; never overwrite baseline reports."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest

ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT))

def main():
    stream=io.StringIO()
    suite=unittest.defaultTestLoader.discover(str(ROOT),pattern='test_*.py')
    with contextlib.redirect_stdout(stream),contextlib.redirect_stderr(stream):
        result=unittest.TextTestRunner(stream=stream,verbosity=2).run(suite)
    sources={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(ROOT.glob('*.py'))}
    digest=hashlib.sha256(''.join(f'{k}\0{v}\n' for k,v in sources.items()).encode()).hexdigest()
    report={'scope':'SYNTHETIC_ACCOUNTING_AND_INGESTION_REGRESSIONS_NOT_PINE_RUNTIME',
            'tests_run':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),
            'successful':result.wasSuccessful(),'command':[sys.executable,*sys.argv],
            'git_head_at_test':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
            'commit_qualification':'Diagnostics may be uncommitted; executable source hashes below identify the exact tested files',
            'tested_accounting_source_sha256':digest,'files_sha256':sources}
    (ROOT/'tests.log').write_text(stream.getvalue(),encoding='utf-8',newline='\n')
    (ROOT/'tests.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8',newline='\n')
    print(json.dumps(report,indent=2))
    return 0 if result.wasSuccessful() else 1

if __name__=='__main__':raise SystemExit(main())
