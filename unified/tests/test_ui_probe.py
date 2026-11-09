"""The temporary parity observer may never replace production or evidence."""
from pathlib import Path
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[2]
GENERATOR=ROOT/'unified/tools/make_ui_probe.py'


class UIProbeSafety(unittest.TestCase):
    def test_new_buffer_preserves_entire_current_source_and_has_no_new_orders(self):
        source=(ROOT/'unified/QIRVANTA_UNIFIED.pine').read_bytes()
        with tempfile.TemporaryDirectory() as folder:
            for minutes in (5,15,30):
                with self.subTest(timeframe_minutes=minutes):
                    output=Path(folder)/'new'/f'observer-{minutes}.txt'
                    optional=[] if minutes==15 else ['--timeframe-minutes',str(minutes)]
                    result=subprocess.run([sys.executable,'-B',str(GENERATOR),str(output),*optional],cwd=ROOT,capture_output=True)
                    self.assertEqual(result.returncode,0,result.stderr.decode())
                    data=output.read_bytes()
                    self.assertTrue(data.startswith(source))
                    appendix=data[len(source):].decode('utf-8')
                    for call in ('strategy.entry(', 'strategy.exit(', 'strategy.close(', 'strategy.cancel('):
                        self.assertNotIn(call,appendix)
                    meta=json.loads(output.with_suffix('.manifest.json').read_text(encoding='utf-8'))
                    self.assertEqual(meta['source_sha256'],hashlib.sha256(source).hexdigest())
                    self.assertEqual(meta['editor_buffer_sha256'],hashlib.sha256(data).hexdigest())
                    self.assertEqual(meta['timeframe_minutes'],minutes)
                    cutoff='02:30' if minutes==30 else '02:45'
                    self.assertEqual(meta['cutoff_utc'],f'2026-11-09T{cutoff}:00Z')
                    self.assertIn('"ui_only|" + runModel',appendix)
                    self.assertIn(f'09 Nov 2026 {cutoff} +0000',appendix)
                    self.assertIn(f'if barstate.isfirst and timeframe.in_seconds() != {minutes*60}',appendix)

    def test_existing_outputs_and_production_extension_refused_even_optimized(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            for optimized in (False,True):
                for filename in ('keep.txt','production.pine'):
                    with self.subTest(optimized=optimized,filename=filename):
                        output=root/filename
                        output.write_bytes(b'IMMUTABLE EVIDENCE')
                        command=[sys.executable,'-B']+(['-O'] if optimized else [])+[str(GENERATOR),str(output)]
                        result=subprocess.run(command,cwd=ROOT,capture_output=True)
                        self.assertNotEqual(result.returncode,0)
                        self.assertEqual(output.read_bytes(),b'IMMUTABLE EVIDENCE')


if __name__=='__main__':
    unittest.main()
