"""Apply the logged statistical-only correction without rerunning any fills."""
import gzip
import json
from research.validation.protocol import ROOT, REPORTS, sha
from research.validation.statistics import block_bootstrap


def correct():
    original = REPORTS/'method_history/experiment_results_initial.json'
    amendment = json.loads((REPORTS/'small_sample_correction_registration.json').read_text('utf-8'))
    if sha(original) != amendment['original_results_sha256']:
        raise ValueError('Original results changed')
    result = json.loads(original.read_text('utf-8'))
    affected = []
    for trial in result['trials']:
        journal = ROOT/trial['journal']
        if sha(journal) != trial['journal_sha256']:
            raise ValueError('Economic journal changed')
        data = json.loads(gzip.decompress(journal.read_bytes()))
        complete = sorted((p for p in data['plans'] if p['status']=='COMPLETE'),key=lambda p:(p['entry_time'],p['plan_id']))
        rs = [p['net_r'] for p in complete if p['net_r'] is not None]
        if 0 < len(rs) <= 5:
            trial['mean_r_uncertainty'] = block_bootstrap(rs)
            affected.append(trial['trial']['id'])
    result['statistical_correction'] = {'registration_sha256':sha(REPORTS/'small_sample_correction_registration.json'), 'corrected_statistics_sha256':sha(ROOT/'research/validation/statistics.py'), 'affected_trials':affected, 'economic_journals_unchanged':True,'original_results_sha256':sha(original)}
    (REPORTS/'experiment_results.json').write_text(json.dumps(result,indent=2,ensure_ascii=False,allow_nan=False)+'\n','utf-8')
    return affected


if __name__=='__main__':
    print(correct())
