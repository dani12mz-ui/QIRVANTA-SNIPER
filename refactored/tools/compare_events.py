#!/usr/bin/env python3
"""Compare exported technical candidates by stable id and fixed prices, not just net R."""
import argparse,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tools.validate import load_events
FIELDS=('plan_id','family','side','known_time','effective_time','reference','initial_sl','tp1','tp2','tp3')
def compare(a,b):
    def normalize(rows):
        result={}
        for e in rows:
            if e.get('event')=='CONFIRMED' and e.get('book')=='CORE':
                pid=e['plan_id']
                if pid in result:raise ValueError('Repeated plan id')
                result[pid]={k:e.get(k) for k in FIELDS}
        return result
    aa,bb=normalize(a),normalize(b)
    return {'a_count':len(aa),'b_count':len(bb),'only_a':sorted(aa.keys()-bb.keys()),'only_b':sorted(bb.keys()-aa.keys()),'changed':[{'id':k,'a':aa[k],'b':bb[k]} for k in sorted(aa.keys()&bb.keys()) if aa[k]!=bb[k]],'empty_comparison':not aa and not bb}
def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('a',type=Path);ap.add_argument('b',type=Path);ap.add_argument('--out',type=Path,default=Path('reports/event_comparison.json'));args=ap.parse_args()
    r=compare(load_events(args.a),load_events(args.b));r['match']=not(r['only_a'] or r['only_b'] or r['changed'] or r['empty_comparison']);args.out.parent.mkdir(parents=True,exist_ok=True);args.out.write_text(json.dumps(r,ensure_ascii=False,indent=2));print(json.dumps(r));return 0 if r['match'] else 1
if __name__=='__main__':raise SystemExit(main())
