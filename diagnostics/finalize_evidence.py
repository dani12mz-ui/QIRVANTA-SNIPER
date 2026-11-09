"""Verify preserved baseline bytes and freeze the actual diagnostic deliverables."""
from pathlib import Path
import hashlib
import json
import re
import subprocess
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
D = ROOT / 'diagnostics'

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8', newline='\n')

frozen = json.loads((D/'protocol/baseline_manifest.json').read_text(encoding='utf-8'))
changed = [f['path'] for f in frozen['files'] if sha(ROOT/f['path']) != f['working_tree_sha256_at_freeze']]
if changed:
    raise RuntimeError(f'Baseline changed: {changed}')
logs = {'warnings':D/'warnings/verified_tests.log', 'accounting':D/'accounting/tests.log', 'diagnosis':D/'diagnosis/verified_tests.log', 'protocol':D/'protocol/H1_integrity_tests.log'}
counts = {}
for suite, path in logs.items():
    text = path.read_text(encoding='utf-8')
    matches = re.findall(r'Ran (\d+) tests', text)
    if not matches or '\nOK' not in text or 'FAILED (' in text:
        raise RuntimeError(f'No successful test evidence: {path}')
    counts[suite] = {'count':int(matches[-1]), 'log':str(path.relative_to(D)).replace('\\','/'), 'sha256':sha(path)}
status = {
    'completed_at_utc': datetime.now(timezone.utc).isoformat(),
    'baseline_commit':frozen['baseline_commit'],
    'baseline_files_verified_unchanged':len(frozen['files']),
    'production_pine_changed':False,
    'production_candidate_adopted':False,
    'baseline_tests':{'unified':99,'refactored':127,'evidence':'protocol/baseline_tests.json'},
    'new_tests':counts,
    'new_tests_total':sum(x['count'] for x in counts.values()),
    'baseline_actual':{'complete_positions':72,'closed_parts':212,'net_usd':-144.492,'native_dd_usd':842.255},
    'H1_actual':{'complete_positions':35,'net_usd':122.428,'later_observed_cohort_net_usd':-65.720,'net_R':-0.58593,'decision':'NOT_ADOPTED_SMALL_FAST_SAMPLE_AND_UNSTABLE_LATER_PORTFOLIO'},
    'warning_scope':'4 x 6445 actual historical calls; zero direct-history/predicate mismatches; native journal unchanged; warnings retained',
    'mobile_active_plan_sl_tp':'NOT_VERIFIED_NO_CURRENT_ACTIVE_PLAN_AND_INTRADAY_REPLAY_PLAN_LIMIT',
    'price_notification_delivery':'PASS_WEB_TOAST_AND_LOG_2026-11-09T03:27:22Z',
    'script_notification_delivery':'NOT_VERIFIED_BASIC_HAS_ZERO_TECHNICAL_ALERTS',
    'physical_phone_delivery':'NOT_VERIFIED',
    'untouched_future_holdout':'NOT_AVAILABLE_YET_NOT_RUN',
    'real_broker_orders':False,
    'final_tradingview_script':'QIRVANTA_UNIFIED version 9; exact baseline editor hash restored',
    'limits':['Earlier old-run feed cannot be retroactively authenticated from summary numbers','Historical chronological split is already observed, not untouched OOS','Per-candidate Properties dialogs were not independently captured; declaration unchanged, no Properties edits, final Properties match original','No tick-level intrabar replay or live broker-fill validation'],
}
write(D/'STATUS.json', status)
files = subprocess.check_output(['git','ls-files','--cached','--others','--exclude-standard','--','diagnostics'], cwd=ROOT).decode().splitlines()
entries = {p:{'sha256':sha(ROOT/p),'bytes':(ROOT/p).stat().st_size} for p in sorted(set(files)) if p not in ['diagnostics/EVIDENCE_SHA256.json','diagnostics/PUBLICATION.json']}
write(D/'EVIDENCE_SHA256.json', {'scope':'Exact repository deliverable bytes; excludes self and post-publication metadata','baseline_preserved':True,'files':entries})
print(json.dumps({'baseline_files_unchanged':len(frozen['files']),'tests':sum(x['count'] for x in counts.values()),'evidence_files':len(entries),'bytes':sum(x['bytes'] for x in entries.values())}))
