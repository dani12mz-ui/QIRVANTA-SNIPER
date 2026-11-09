"""Exact protected-source projection against the frozen unified engineering base.

This proves source identity, not TradingView compilation, runtime equivalence,
delivery of alerts, or financial improvement. No baseline files are rewritten.
"""
from pathlib import Path
import argparse
import hashlib
import json
import subprocess

ROOT = Path(__file__).resolve().parents[1]
BASELINE = 'dd1819515b6fe27e2a08bff91c1fbad6bfd7bcc9'
EXACT = ('native_header', 'features', 'types_helpers', 'decision',
         'paper_engine', 'native_adapter', 'alerts')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def baseline(name):
    return subprocess.check_output(
        ['git', 'show', f'{BASELINE}:unified/src/{name}.pineinc'], cwd=ROOT.parent)


def current(name):
    return (ROOT / 'src' / f'{name}.pineinc').read_bytes()


def protected_inputs(data):
    # Exclude only whole input declarations in the presentation group. All
    # decision, cost, alert inputs AND derived execution checks remain protected.
    return b''.join(line for line in data.splitlines(keepends=True)
                    if not (b'input.' in line and 'group="06 / ИЗГЛЕД"'.encode() in line))


def protected_routes(data):
    return data.split(b'f_isRiskEvent(Event e) =>\n', 1)[1].split(
        b'f_readableEvent(Event e) =>\n', 1)[0]


def projection(parts):
    # Explicit framing prevents ambiguous concatenation of names and sources.
    return b''.join(name.encode() + b'\0' + str(len(value)).encode() + b'\0' + value
                    for name, value in parts.items())


def check():
    old = {name: baseline(name) for name in EXACT}
    new = {name: current(name) for name in EXACT}
    old['inputs_without_display'] = protected_inputs(baseline('inputs'))
    new['inputs_without_display'] = protected_inputs(current('inputs'))
    old['alert_admission_routes'] = protected_routes(baseline('alerts_helpers'))
    new['alert_admission_routes'] = protected_routes(current('alerts_helpers'))
    components = {name: {'baseline_sha256': digest(old[name]),
                         'current_sha256': digest(new[name]),
                         'byte_identical': old[name] == new[name]}
                  for name in old}
    differences = [name for name, item in components.items() if not item['byte_identical']]
    return {
        'status': 'PASS' if not differences else 'FAIL',
        'baseline_commit': BASELINE,
        'scope': 'EXACT_SOURCE_PROJECTION_NOT_PINE_RUNTIME_PROOF',
        'components': components,
        'baseline_projection_sha256': digest(projection(old)),
        'current_projection_sha256': digest(projection(new)),
        'differences': differences,
        'allowed_changes': ['06 presentation inputs', 'display_context',
                            'visual rendering', 'human alert formatting'],
        'limitations': ['Pine compilation and matched runtime journals are separate evidence.',
                        'CORE_SHA256 includes display input text, so its change alone is not an economic change.',
                        'Alert routing is identical; human message payloads intentionally differ.'],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--write-report', action='store_true')
    args = parser.parse_args()
    result = check()
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
    if args.write_report:
        (ROOT / 'reports' / 'engineering_logic_parity.json').write_text(
            rendered, encoding='utf-8', newline='\n')
    print(rendered, end='')
    return bool(result['differences'])


if __name__ == '__main__':
    raise SystemExit(main())
