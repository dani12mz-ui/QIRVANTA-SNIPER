"""Artifact/provenance and static UI contracts; these do not compile Pine."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from tools import build

ROOT=Path(__file__).resolve().parents[1]
CORE_HASH='c189ef479ccc106178a8a3b146da0dcd7d2fac852ddbeb8eebb298f385748dcc'

def core(source):
    return source.split('// BEGIN CANONICAL CORE ',1)[1].split('\n',1)[1].split('// END CANONICAL CORE\n',1)[0]

class DeliveryIntegrity(unittest.TestCase):
    def test_initial_audited_bytes_preserved(self):
        manifest=json.loads((ROOT/'baseline/delivery_manifest.json').read_text(encoding='utf-8'))
        for filename in build.OUTPUTS:
            data=(ROOT/'baseline/audited'/filename).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(),manifest['files_sha256_lf_git']['audited/'+filename])
        self.assertEqual(hashlib.sha256((ROOT/'baseline/delivery_103bc307.zip').read_bytes()).hexdigest(),manifest['archive_sha256'])

    def test_candidate_and_execution_core_identical_to_latest_baseline(self):
        sources,digest=build.assemble()
        self.assertEqual(digest,CORE_HASH)
        for filename,source in sources.items():
            self.assertEqual(core(source),core((ROOT/'baseline/audited'/filename).read_text(encoding='utf-8')))

    def test_source_recovery_evidence_matches_baseline(self):
        evidence=json.loads((ROOT/'reports/source_recovery.json').read_text(encoding='utf-8'))
        for filename,record in evidence['files'].items():
            data=(ROOT/'baseline/audited'/filename).read_text(encoding='utf-8').encode('utf-8')
            self.assertEqual(hashlib.sha256(data).hexdigest(),record['sha256_lf'])

    def test_check_detects_crlf_without_repairing_or_overwriting(self):
        sources,_=build.assemble()
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)/'refactored'
            audited=Path(td)/'audited'
            root.mkdir();audited.mkdir()
            for directory in (root,audited):
                for filename,source in sources.items():
                    (directory/filename).write_bytes(source.encode('utf-8'))
            with patch.object(build,'ROOT',root),contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(build.build(check=True),0)
                target=audited/'QIRVANTA_SNIPER_VALIDATION.pine'
                altered=target.read_bytes().replace(b'\n',b'\r\n')
                target.write_bytes(altered)
                self.assertEqual(build.build(check=True),1)
                self.assertEqual(target.read_bytes(),altered)

    def test_ui_is_outside_canonical_candidate_core(self):
        sources,_=build.assemble()
        for source in sources.values():
            self.assertNotIn('showConfirmedLabels',core(source))
            self.assertNotIn('f_direction',core(source))

    def test_unconfirmed_display_uses_neutral_direction_static(self):
        visual=build.text('visual_alerts')
        for line in visual.splitlines():
            if any(x in line for x in ('string idleStage','string bothDiagnostics','string name = chosenEvent','label.set_text(setupLabel')):
                self.assertNotIn('f_side(',line)
                self.assertNotIn('КУПИ',line)
                self.assertNotIn('ПРОДАЙ',line)

    def test_mobile_prices_and_target_classification_static(self):
        visual=build.text('visual_alerts')
        line=next(x for x in visual.splitlines() if 'string riskLine =' in x)
        for token in ('shownEntry','shownPlan.initialStop','shownStop','shownPlan.tp1','shownPlan.tp2','shownPlan.tp3','shownPlan.obstacle'):
            self.assertIn(token,line)
        self.assertIn('R ПРОЕКЦИИ',visual)
        self.assertIn('dd.MM HH:mm',visual)

    def test_native_standard_detail_compatible_default_only(self):
        current=build.text('native_header').splitlines()[1]
        baseline=(ROOT/'baseline/audited/QIRVANTA_SNIPER_VALIDATION.pine').read_text(encoding='utf-8').splitlines()[1]
        self.assertIn('use_bar_magnifier=false',current)
        self.assertEqual(current.replace('shorttitle="QIRV TEST"','shorttitle="QIRVANTA TEST"').replace('use_bar_magnifier=false','use_bar_magnifier=true'),baseline)

if __name__=='__main__':unittest.main()
