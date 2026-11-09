"""Run focused validation/model/label/risk tests and record provenance."""
import io
import json
import sys
import unittest
from research.validation.protocol import ROOT, REPORTS, sha


def main():
    suite=unittest.defaultTestLoader.discover(str(ROOT/'research/tests'),pattern='test_validation*.py')
    stream=io.StringIO()
    result=unittest.TextTestRunner(stream=stream,verbosity=2).run(suite)
    sources=[p for name in ('validation','models','labeling','risk') for p in (ROOT/'research'/name).glob('*.py')]
    sources+=list((ROOT/'research/tests').glob('test_validation*.py'))
    report={'schema':'qirvanta.research.validation_tests.v1','tests':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),'skipped':len(result.skipped),'passed':result.wasSuccessful(),'python':sys.version,'scope':'purging, embargo, no future-label fitting, causal metadata, ambiguous labels, excursion bounds, uncertainty, broker fail-closed sizing; not profitability','source_sha256':{p.relative_to(ROOT).as_posix():sha(p) for p in sorted(sources)}}
    (REPORTS/'validation_tests.log').write_text(stream.getvalue(),'utf-8')
    (REPORTS/'validation_tests.json').write_text(json.dumps(report,indent=2)+'\n','utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='source_sha256'}))
    return 0 if result.wasSuccessful() else 1


if __name__=='__main__':raise SystemExit(main())
