"""Run synthetic regressions and record exact implementation/test provenance."""
from pathlib import Path
import hashlib
import io
import json
import sys
import unittest

ROOT=Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))


def main():
    suite=unittest.defaultTestLoader.discover(str(ROOT/'research/tests'),pattern='test_engine*.py')
    stream=io.StringIO()
    result=unittest.TextTestRunner(stream=stream,verbosity=2).run(suite)
    paths=[]
    for folder in ('data','features','strategies','backtesting'):
        paths.extend((ROOT/'research'/folder).glob('*.py'))
    paths.extend((ROOT/'research/tests').glob('test_engine*.py'))
    hashes={p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths)}
    report={'scope':'SYNTHETIC_ENGINE_REGRESSIONS_NOT_NATIVE_PARITY_OR_PROFITABILITY','tests':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),'skipped':len(result.skipped),'passed':result.wasSuccessful(),'python':sys.version,'source_sha256':hashes}
    (ROOT/'research/backtesting/tests.log').write_text(stream.getvalue(),encoding='utf-8')
    (ROOT/'research/backtesting/tests.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='source_sha256'}))
    return 0 if result.wasSuccessful() else 1


if __name__=='__main__':raise SystemExit(main())
