"""Verify retained economics or deterministically replay them without overwriting.

The original one-shot run_experiments registration remains intentionally frozen;
this entry point uses the final code plus recorded economic configuration and
requires unchanged economic source hashes. Statistical corrections do not
silently authorize a different trading algorithm.
"""
import argparse
import gzip
import hashlib
import json
from pathlib import Path

from research.backtesting.engine import BacktestConfig, run_backtest
from research.data.aggregate import aggregate_m15_to_m30
from research.data.loader import load_bars
from research.validation.protocol import ROOT, REPORTS, sha


def load_evidence(trial, public_only=False):
    path=ROOT/Path(trial['journal'].replace('\\','/'))
    if path.exists() and not public_only:
        if sha(path)!=trial['journal_sha256']:
            raise ValueError('Journal hash mismatch: '+trial['trial']['id'])
        return json.loads(gzip.decompress(path.read_bytes())), False
    from research.validation.public_evidence import load_public_journal
    return load_public_journal(trial['journal_sha256']), True


def replay_equal(replayed, saved, sparse):
    if sparse:
        from research.validation.public_evidence import sanitize_journal, canonical_json_bytes
        return canonical_json_bytes(sanitize_journal(replayed))==canonical_json_bytes(saved)
    return replayed==saved


def publication_snapshot():
    from research.validation.public_evidence import POLICY_ID, DEFAULT_MANIFEST
    return {'policy_id':POLICY_ID,'manifest_sha256':sha(DEFAULT_MANIFEST),
            'public_adapter_sha256':sha(ROOT/'research/validation/public_evidence.py')}


def assert_publication_unchanged(snapshot):
    if snapshot is not None and publication_snapshot()!=snapshot:
        raise ValueError('Publication policy or manifest changed during verification')


def verify(execute=False, public_only=False):
    frozen=json.loads((REPORTS/'final_reproduction_manifest.json').read_text('utf-8'))
    hashes={p.replace('\\','/'):h for p,h in frozen['source_sha256'].items()}
    for path in ('research/data/loader.py','research/data/aggregate.py','research/features/causal.py','research/strategies/library.py','research/backtesting/engine.py'):
        if sha(ROOT/path)!=hashes[path]:
            raise ValueError('Economic code changed: '+path)
    m15=json.loads((REPORTS/'experiment_results.json').read_text('utf-8'))
    m30=json.loads((REPORTS/'m30_results.json').read_text('utf-8'))
    bars=load_bars() if execute else None
    derived=aggregate_m15_to_m30(bars)[0] if execute else None
    results=[]
    publication=publication_snapshot() if public_only else None
    for group in (m15,m30):
        for trial in group['trials']:
            saved,sparse=load_evidence(trial,public_only)
            if sparse and publication is None:
                publication=publication_snapshot()
            for p in saved['plans']:
                qty=sum(x['quantity'] for x in p['exits'])+p['remaining_quantity']
                if abs(qty-p['quantity'])>1e-8:
                    raise ValueError('Quantity conservation failure')
                if p['status']=='COMPLETE' and abs(sum(x['net_cash'] for x in p['exits'])-p['net_cash'])>1e-7:
                    raise ValueError('Completed campaign net mismatch')
            if saved['summary']!=trial['summary']:
                raise ValueError('Summary differs from journal')
            if execute:
                replay=run_backtest(derived if group is m30 else bars,BacktestConfig(**saved['config']))
                if not replay_equal(replay,saved,sparse):
                    raise ValueError('Deterministic economic replay differs: '+trial['trial']['id'])
            results.append({'id':trial['trial']['id'],'journal_verified':True,'public_sparse_evidence':sparse,'economic_replay_equal':True if execute else None})
    assert_publication_unchanged(publication)
    return {'schema':'qirvanta.research.replay.v1','status':'PASS','scope':'90 frozen economic journals; deterministic rerun' if execute else '90 retained economic journals verified without rerun','economic_source_unchanged':True,'public_evidence':publication,'trials':results,'count':len(results),'native_parity':False,'profitability_validation':False}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute',action='store_true')
    parser.add_argument('--public-only',action='store_true',help='Verify sparse published evidence even if full local journals exist')
    parser.add_argument('--output',type=Path,required=True,help='New verification artifact; never an original result path')
    args=parser.parse_args()
    if args.output.exists():
        raise FileExistsError('Choose a new verification output; original results are immutable')
    report=verify(args.execute,args.public_only)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,indent=2)+'\n','utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='trials'}))
