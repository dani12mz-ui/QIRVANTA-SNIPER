#!/usr/bin/env python3
"""Run local regression tests and record scope explicitly. This does not compile Pine."""
import contextlib,io,json,sys,time,unittest,platform
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from tools.provenance import source_fingerprint,git_commit
def main():
    suite=unittest.defaultTestLoader.discover(str(ROOT/'tests'))
    buffer=io.StringIO();start=time.time()
    with contextlib.redirect_stdout(buffer),contextlib.redirect_stderr(buffer):
        result=unittest.TextTestRunner(stream=buffer,verbosity=2).run(suite)
    report={'scope':'PYTHON_REFERENCE_AND_STATIC_PINE_SOURCE_NOT_PINE_RUNTIME','tests_run':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),'skipped':len(result.skipped),'successful':result.wasSuccessful(),'duration_seconds':round(time.time()-start,3),'pine_compile':'UNVERIFIED','execution_runtime_parity':'UNVERIFIED','market_profitability':'UNVERIFIED_MISSING_DATA'}
    digest,files=source_fingerprint()
    report.update({'tested_code_commit':git_commit(),'tested_source_sha256':digest,'python':sys.version,'platform':platform.platform(),'command':[sys.executable,*sys.argv],'exit_code':0 if result.wasSuccessful() else 1,'source_files_sha256':files})
    (ROOT/'reports/unit_tests.log').write_text(buffer.getvalue(),encoding='utf-8')
    (ROOT/'reports/tests.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='source_files_sha256'},indent=2));return 0 if result.wasSuccessful() else 1
if __name__=='__main__':raise SystemExit(main())
