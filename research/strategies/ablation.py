"""Registered research-only gate removal, without editing the frozen library.

Use in an otherwise single-threaded offline process. The runner must record the
ablation identity and source hash in addition to the unchanged base config hash.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import threading

from research.strategies.library import CandidateLibrary

ALLOWED={'S3':'CONFIRMATION_CANDLE','S5':'CONFIRMATION_CANDLE','S8':'PRIOR_PAUSE'}
_LOCK=threading.Lock()


@dataclass(frozen=True)
class AblationSpec:
    family:str
    gate:str

    def __post_init__(self):
        if ALLOWED.get(self.family)!=self.gate:
            raise ValueError('Only the explicitly registered family/gate ablations are supported')


class AblatedCandidateLibrary(CandidateLibrary):
    def __init__(self,params=None,mintick=.001,*,family,gate):
        self.ablation=AblationSpec(family,gate)
        super().__init__(params,mintick)

    def evaluate(self,family,side,i,bars,features):
        decision=super().evaluate(family,side,i,bars,features)
        if family!=self.ablation.family or not decision.get('raw_setup') or decision.get('status')!='REJECTED' or self.ablation.gate not in decision['reasons']:
            return decision
        # The original library has already advanced/consumed the setup. No second
        # evaluation, restart or hindsight change of its state is permitted.
        original=list(decision['reasons'])
        decision['reasons']=[reason for reason in original if reason!=self.ablation.gate]
        decision['ablation_removed_gate']=self.ablation.gate
        decision['ablation_original_reasons']=original
        if not decision['reasons']:
            decision['status']='CONFIRMED'
        return decision


@contextmanager
def gate_ablation(family,gate):
    """Temporarily replace only the library factory; always restore after errors."""
    from research.backtesting import engine
    spec=AblationSpec(family,gate)
    if threading.active_count()!=1:
        raise RuntimeError('Ablation requires a dedicated single-threaded process')
    if not _LOCK.acquire(blocking=False):
        raise RuntimeError('Nested or concurrent ablation is not supported')
    original=engine.CandidateLibrary
    try:
        if original is not CandidateLibrary:
            raise RuntimeError('Unexpected existing research library override')
        def factory(params=None,mintick=.001):
            return AblatedCandidateLibrary(params,mintick,family=spec.family,gate=spec.gate)
        engine.CandidateLibrary=factory
        try:
            yield spec
        finally:
            engine.CandidateLibrary=original
    finally:
        _LOCK.release()
