"""Source/config preservation check. Never claims execution of Pine or discovers signals."""
from pathlib import Path
import hashlib
import json
import re

ROOT=Path(__file__).resolve().parents[1]
BASELINE=ROOT.parent/'refactored'
CORE_MODULES=('features','types_helpers','decision','paper_engine')
PLAN_METADATA_ADDITIONS={
    'int maxHoldBarsFrozen = na',
    'float maxDeviationATRFrozen = na',
    'int expiresAfterBar = na',
    'string cancellationReason = ""',
    '// Additive immutable metadata; candidate thresholds and all prior Plan values stay unchanged.',
    'p.maxHoldBarsFrozen := maxHoldBars',
    'p.maxDeviationATRFrozen := maxOpenDeviationATR',
    'p.expiresAfterBar := bar_index + 1',
    'p.cancellationReason := "Неизпълнена заявка до следващото затваряне"',
}

def arguments(value):
    parts=[];start=0;depth=0;quote=None;escape=False
    for i,c in enumerate(value):
        if quote:
            if escape:escape=False
            elif c=='\\':escape=True
            elif c==quote:quote=None
        elif c in ('"',"'"):quote=c
        elif c in '([{':depth+=1
        elif c in ')]}':depth-=1
        elif c==',' and depth==0:
            parts.append(value[start:i].strip());start=i+1
    parts.append(value[start:].strip())
    return parts

def signal_inputs(source):
    records={}
    for line in source.splitlines():
        m=re.match(r'^(?:bool|int|float|string) (\w+) = (input\.\w+)\((.*)\)$',line)
        if not m or not re.search(r'group="0[1-5] / ',line):continue
        args=arguments(m[3])
        # Display titles/tooltips/groups are excluded; options retain their exact values.
        semantic=[args[0],*[a for a in args[1:] if re.match(r'(minval|maxval|step|options|confirm)\s*=',a)]]
        records[m[1]]={'type':m[2],'contract':semantic}
    return records

def check(baseline=BASELINE,current=ROOT):
    differences=[];modules={}
    for name in CORE_MODULES:
        a=(baseline/'src'/f'{name}.pineinc').read_text(encoding='utf-8')
        b=(current/'src'/f'{name}.pineinc').read_text(encoding='utf-8')
        normalized=''.join(line for line in b.splitlines(keepends=True) if not(name=='types_helpers' and line.strip() in PLAN_METADATA_ADDITIONS))
        modules[name]={'baseline_sha256_lf':hashlib.sha256(a.encode()).hexdigest(),'unified_sha256_lf':hashlib.sha256(b.encode()).hexdigest(),'identical':a==b,'candidate_semantics_identical':a==normalized}
        if a!=normalized:differences.append(name)
    old=(baseline/'src/inputs.pineinc').read_text(encoding='utf-8')
    new=(current/'src/inputs.pineinc').read_text(encoding='utf-8')
    a=signal_inputs(old);b=signal_inputs(new)
    changed=[k for k in sorted(a.keys()|b.keys()) if a.get(k)!=b.get(k)]
    differences += ['input:'+k for k in changed]
    # Derived horizon/session/date/risk logic also participates in candidate discovery.
    derived_equal=old.split('int tfSeconds =',1)[1]==new.split('int tfSeconds =',1)[1]
    if not derived_equal:differences.append('derived_input_logic')
    return {'status':'PASS_SOURCE_AND_CONFIGURATION' if not differences else 'FAIL_SOURCE_OR_CONFIGURATION_CHANGED',
            'scope':'STATIC_PINE_SIGNAL_SOURCE_IDENTITY_NOT_RUNTIME_PARITY',
            'modules':modules,'signal_input_count':len(a),'changed_signal_inputs':changed,
            'derived_input_logic_identical':derived_equal,'differences':differences,
            'documented_exception':'types_helpers: additive frozen Plan lifetime/deviation metadata; exact approved lines excluded, all prior fields and calculations retained',
            'pine_runtime_signal_parity':'UNVERIFIED_UNTIL_MATCHED_PINE_EVENT_EXPORTS',
            'synthetic_reference':'See tests for identical supplied-candidate reference replay; this is not OHLC-to-signal Pine evaluation.'}

def main():
    result=check();(ROOT/'reports').mkdir(exist_ok=True)
    (ROOT/'reports/signal_parity.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return bool(result['differences'])

if __name__=='__main__':raise SystemExit(main())
