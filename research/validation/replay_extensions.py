"""Verify/replay 39 registered extensions; public sparse evidence supported.

Actual-TF execution requires separately authorized local datasets pinned by hash.
No full raw history is downloaded, imputed or copied into the repository.
"""
import argparse
import json
from contextlib import nullcontext
from pathlib import Path

from research.backtesting.engine import BacktestConfig, run_backtest
from research.data.loader import load_bars
from research.strategies.ablation import gate_ablation
from research.validation.protocol import ROOT, REPORTS, sha
from research.validation.replay import load_evidence, replay_equal, publication_snapshot, assert_publication_unchanged
from research.validation.run_extensions import add_variant_metadata


def verify(execute=False, public_only=False):
    frozen=json.loads((REPORTS/'extension_source_freeze.json').read_text('utf-8'))
    if any(sha(ROOT/p)!=h for p,h in frozen['economic_source_sha256'].items()):
        raise ValueError('Economic sources changed')
    if sha(ROOT/'research/strategies/ablation.py')!=frozen['ablation_adapter_sha256']:
        raise ValueError('Ablation implementation changed')
    actual=json.loads((REPORTS/'actual_tf_results.json').read_text('utf-8'))
    ablation=json.loads((REPORTS/'ablation_results.json').read_text('utf-8'))
    data={}
    if execute:
        for source in actual['sources']:
            path=ROOT/source['local_csv']
            if not path.exists():
                raise FileNotFoundError('Actual-TF raw data are local-only; supply the authorized file pinned in actual_tf_registration.json: '+source['local_csv'])
            if sha(path)!=source['csv_sha256']:
                raise ValueError('Actual timeframe data changed')
            data[source['timeframe']]=[b for b in load_bars(path) if b['time_close']<=actual['common_window']['end']]
        original=load_bars()
    comparisons=[]
    publication=publication_snapshot() if public_only else None
    for group in (actual,ablation):
        for t in group['trials']:
            saved,sparse=load_evidence(t,public_only)
            if sparse and publication is None:
                publication=publication_snapshot()
            if saved['summary']!=t['summary'] or saved['research_variant']!=t['variant_identity']:
                raise ValueError('Summary/variant identity mismatch')
            if execute:
                trial=t['trial']
                context=gate_ablation(trial['family'],trial['remove_gate']) if trial.get('remove_gate') else nullcontext()
                with context:
                    result=run_backtest(data[trial['timeframe']] if group is actual else original,BacktestConfig(**saved['config']))
                result=add_variant_metadata(result,trial,frozen)
                if not replay_equal(result,saved,sparse):
                    raise ValueError('Extension deterministic replay differs: '+trial['id'])
            comparisons.append({'id':t['trial']['id'],'journal_verified':True,'public_sparse_evidence':sparse,'economic_replay_equal':True if execute else None})
    assert_publication_unchanged(publication)
    return {'schema':'qirvanta.research.extension_replay.v1','status':'PASS','count':len(comparisons),'execute':execute,'public_evidence':publication,'trials':comparisons,'native_parity':False,'profitability_validation':False}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--execute',action='store_true')
    p.add_argument('--public-only',action='store_true')
    p.add_argument('--output',type=Path,required=True)
    args=p.parse_args()
    if args.output.exists():
        raise FileExistsError('Use a new verification output; do not overwrite historical reports')
    report=verify(args.execute,args.public_only)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,indent=2)+'\n','utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='trials'}))
