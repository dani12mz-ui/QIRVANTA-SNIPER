"""Generate the one preregistered exploratory variant; never adopt it automatically."""
from pathlib import Path
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
registration = ROOT / 'diagnostics/diagnosis/H1_PREREGISTRATION.json'
if hashlib.sha256(registration.read_bytes()).hexdigest() != '2b26022eb385d7f4a3574b8792b9ec9eb6d9d844b5953e27147fc891783f92c5':
    raise ValueError('Preregistration hash changed')
text = Path(sys.argv[1]).read_text(encoding='utf-8')
if hashlib.sha256(text.encode()).hexdigest() != '430352649bbb906f6723c947a7f00f1e5213715d7eac5724ef47d883c0529aa9':
    raise ValueError('Observer appendix hash changed')
base = subprocess.check_output(['git', 'show', '48e5cddab17ea831bdc277f17f0406329fdfe39c:unified/QIRVANTA_UNIFIED.pine'], cwd=ROOT).decode()
if not text.startswith(base):
    raise ValueError('Observer does not preserve the exact baseline prefix')
start, end = base.index('f_fast(int d,'), base.index('f_reversal(Setup s,')
fast = base[start:end]
old_tests = 'not requireRoom or room, not requireFVG or gap)'
new_tests = 'not requireRoom or room, not requireFVG or gap, not na(efficiency) and efficiency >= 0.25)'
old_names = '"Място до TP1", "FVG"))'
new_names = '"Място до TP1", "FVG", "H1: насоченост <0.25"))'
if fast.count(old_tests) != 1 or fast.count(old_names) != 1:
    raise ValueError('FAST edit anchors are absent or ambiguous')
changed = fast.replace(old_tests, new_tests).replace(old_names, new_names)
candidate = base[:start] + changed + base[end:]
sha = hashlib.sha256(candidate.encode()).hexdigest()
appendix = text[len(base):].replace('string dgRun = "baseline|"', 'string dgRun = "H1|"').replace('c6bfe1af0604c447eddaed1db5195bfe2c07d3826e7fafc70113235e9f25f1a5', sha)
# Capture the full observed OHLC/HTF serialization for both candidate runs.
appendix = appendix.replace('if runModel == "Всички"', 'if true')
appendix = appendix.replace('// DIAGNOSTIC OBSERVER ONLY. Baseline above is byte-identical to commit 48e5cdd.', '// H1 exploratory test-only variant. Baseline is preserved at 48e5cdd; do not adopt without prospective validation.')
output = Path(sys.argv[2])
manifest_path = output.with_suffix('.manifest.json')
if output.exists() or manifest_path.exists():
    raise FileExistsError('Refusing to overwrite a generated buffer or manifest')
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(candidate + appendix, encoding='utf-8', newline='\n')
manifest = {'hypothesis':'H1_FAST_EFFICIENCY_025','registration_sha256':hashlib.sha256(registration.read_bytes()).hexdigest(),'baseline_sha256':hashlib.sha256(base.encode()).hexdigest(),'candidate_strategy_sha256':sha,'editor_buffer_sha256':hashlib.sha256((candidate+appendix).encode()).hexdigest(),'changed_model':'FAST','added_gate':'not na(efficiency) and efficiency >= 0.25','all_other_model_and_execution_bytes_unchanged':True,'production_adopted':False}
manifest.update({'input_observer_sha256':hashlib.sha256(text.encode()).hexdigest(),'input_observer_path':str(Path(sys.argv[1])),'registration_time_utc':json.loads(registration.read_text(encoding='utf-8'))['registered_at_utc'],'generated_at_utc':datetime.now(timezone.utc).isoformat()})
manifest_path.write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n', encoding='utf-8',newline='\n')
print(json.dumps(manifest))
