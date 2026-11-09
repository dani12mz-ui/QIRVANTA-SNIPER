"""Independently exercise candidate-generation boundaries in temporary directories."""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
BASELINE = "48e5cddab17ea831bdc277f17f0406329fdfe39c"
BASE_SHA = "c6bfe1af0604c447eddaed1db5195bfe2c07d3826e7fafc70113235e9f25f1a5"
REG_SHA = "2b26022eb385d7f4a3574b8792b9ec9eb6d9d844b5953e27147fc891783f92c5"
OBSERVER_SHA = "430352649bbb906f6723c947a7f00f1e5213715d7eac5724ef47d883c0529aa9"


def sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class H1SingleChangeIntegrity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.baseline = subprocess.check_output(["git", "show", f"{BASELINE}:unified/QIRVANTA_UNIFIED.pine"], cwd=ROOT).decode("utf-8")
        cls.git_dir = subprocess.check_output(["git", "rev-parse", "--absolute-git-dir"], cwd=ROOT).decode().strip()
        cls.registration = (ROOT / "diagnostics/diagnosis/H1_PREREGISTRATION.json").read_bytes()
        cls.generated_workspace = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.generated_workspace.cleanup)
        cls.workspace = Path(cls.generated_workspace.name)
        observer_path = cls.workspace / "observer.txt"
        observed = subprocess.run([sys.executable, "-B", str(ROOT / "diagnostics/make_probe.py"), str(observer_path)],
                                  cwd=ROOT, capture_output=True)
        if observed.returncode:
            raise RuntimeError(observed.stderr.decode(errors="replace"))
        cls.observer = observer_path.read_text(encoding="utf-8")
        start, end = cls.baseline.index("f_fast(int d,"), cls.baseline.index("f_reversal(Setup s,")
        cls.start, cls.end = start, end
        original = cls.baseline[start:end]
        cls.expected_fast = original.replace("not requireRoom or room, not requireFVG or gap)",
                                            "not requireRoom or room, not requireFVG or gap, not na(efficiency) and efficiency >= 0.25)")
        cls.expected_fast = cls.expected_fast.replace('"Място до TP1", "FVG"))', '"Място до TP1", "FVG", "H1: насоченост <0.25"))')
        cls.expected_candidate = cls.baseline[:start] + cls.expected_fast + cls.baseline[end:]
        appendix = cls.observer[len(cls.baseline):]
        cls.expected_appendix = appendix.replace('string dgRun = "baseline|"', 'string dgRun = "H1|"')
        cls.expected_appendix = cls.expected_appendix.replace(BASE_SHA, sha(cls.expected_candidate.encode()))
        cls.expected_appendix = cls.expected_appendix.replace('if runModel == "Всички"', "if true")
        cls.expected_appendix = cls.expected_appendix.replace(
            "// DIAGNOSTIC OBSERVER ONLY. Baseline above is byte-identical to commit 48e5cdd.",
            "// H1 exploratory test-only variant. Baseline is preserved at 48e5cdd; do not adopt without prospective validation.")
        result, candidate_path, generated_manifest_path = cls.invoke(cls.workspace / "candidate")
        if result.returncode:
            raise RuntimeError(result.stderr.decode(errors="replace"))
        cls.generated_candidate_bytes = candidate_path.read_bytes()
        cls.generated_candidate_text = cls.generated_candidate_bytes.decode("utf-8")
        cls.generated_manifest = json.loads(generated_manifest_path.read_text(encoding="utf-8"))

    @classmethod
    def invoke(cls, directory, *, observer=None, registration=None, omit_registration=False, optimized=False):
        temporary = Path(directory)
        diagnosis = temporary / "diagnostics/diagnosis"
        diagnosis.mkdir(parents=True)
        generator = temporary / "diagnostics/make_h1_probe.py"
        shutil.copyfile(ROOT / "diagnostics/make_h1_probe.py", generator)
        if not omit_registration:
            (diagnosis / "H1_PREREGISTRATION.json").write_bytes(cls.registration if registration is None else registration)
        source = temporary / "input.txt"
        source.write_text(cls.observer if observer is None else observer, encoding="utf-8", newline="\n")
        output = temporary / "candidate.txt"
        env = dict(os.environ, GIT_DIR=cls.git_dir, GIT_WORK_TREE=str(temporary), PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
        command = [sys.executable, "-B", *(["-O"] if optimized else []), str(generator), str(source), str(output)]
        result = subprocess.run(command, cwd=temporary, env=env, capture_output=True)
        return result, output, output.with_suffix('.manifest.json')

    def test_actual_artifacts_preserve_exact_baseline_except_registered_fast_gate(self):
        self.assertEqual(sha(self.baseline.encode()), BASE_SHA)
        self.assertEqual(sha(self.registration), REG_SHA)
        self.assertEqual(sha(self.observer.encode()), OBSERVER_SHA)
        actual = self.generated_candidate_text
        self.assertEqual(actual, self.expected_candidate + self.expected_appendix)
        candidate = actual[:len(self.expected_candidate)]
        self.assertEqual(candidate[:self.start], self.baseline[:self.start])
        actual_reversal = candidate.index("f_reversal(Setup s,")
        self.assertEqual(candidate[actual_reversal:], self.baseline[self.end:])
        self.assertEqual(candidate[self.start:actual_reversal], self.expected_fast)
        self.assertEqual(self.expected_fast.count("not na(efficiency) and efficiency >= 0.25"), 1)
        self.assertEqual(self.expected_fast.count('"H1: насоченост <0.25"'), 1)

    def test_actual_manifest_hashes_and_registration_chronology_match_bytes(self):
        manifest = json.loads((ROOT / "diagnostics/diagnosis/H1_GENERATED_v2.json").read_text(encoding="utf-8"))
        registered = json.loads(self.registration)["registered_at_utc"]
        self.assertEqual(manifest["registration_sha256"], REG_SHA)
        self.assertEqual(manifest["registration_time_utc"], registered)
        self.assertGreaterEqual(datetime.fromisoformat(manifest["generated_at_utc"]), datetime.fromisoformat(registered))
        self.assertEqual(manifest["input_observer_sha256"], OBSERVER_SHA)
        self.assertEqual(manifest["baseline_sha256"], BASE_SHA)
        self.assertEqual(manifest["candidate_strategy_sha256"], sha(self.expected_candidate.encode()))
        self.assertEqual(manifest["editor_buffer_sha256"], sha(self.generated_candidate_bytes))
        self.assertEqual(manifest["editor_buffer_sha256"], self.generated_manifest["editor_buffer_sha256"])
        self.assertEqual(manifest["changed_model"], "FAST")
        self.assertFalse(manifest["production_adopted"])

    def test_successful_generator_reproduces_candidate_with_normal_and_optimized_python(self):
        for optimized in (False, True):
            with self.subTest(optimized=optimized), tempfile.TemporaryDirectory() as folder:
                result, output, manifest_path = self.invoke(folder, optimized=optimized)
                self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
                self.assertEqual(output.read_text(encoding="utf-8"), self.expected_candidate + self.expected_appendix)
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                self.assertEqual(manifest["editor_buffer_sha256"], sha(output.read_bytes()))
                self.assertEqual(manifest["registration_sha256"], REG_SHA)
                # Baseline observer creates documented nested output directories, but never replaces evidence.
                observer_path = Path(folder) / "work/deep/observer.txt"
                observer_command = [sys.executable, "-B", *(["-O"] if optimized else []),
                                    str(ROOT / "diagnostics/make_probe.py"), str(observer_path)]
                created = subprocess.run(observer_command, cwd=ROOT, capture_output=True)
                self.assertEqual(created.returncode, 0, created.stderr.decode(errors="replace"))
                self.assertEqual(sha(observer_path.read_bytes()), OBSERVER_SHA)
                original_bytes = observer_path.read_bytes()
                refused = subprocess.run(observer_command, cwd=ROOT, capture_output=True)
                self.assertNotEqual(refused.returncode, 0)
                self.assertIn(b"Refusing to overwrite", refused.stderr)
                self.assertEqual(observer_path.read_bytes(), original_bytes)
                with self.subTest(output_type="existing_directory"):
                    directory = Path(folder) / "existing.txt"
                    directory.mkdir()
                    rejected = subprocess.run(observer_command[:-1] + [str(directory)], cwd=ROOT, capture_output=True)
                    self.assertNotEqual(rejected.returncode, 0)
                    self.assertTrue(directory.is_dir())
                with self.subTest(output_type="pine"):
                    protected = Path(folder) / "production.pine"
                    protected.write_bytes(b"preserve production bytes\n")
                    rejected = subprocess.run(observer_command[:-1] + [str(protected)], cwd=ROOT, capture_output=True)
                    self.assertNotEqual(rejected.returncode, 0)
                    self.assertIn(b"must be a new .txt", rejected.stderr)
                    self.assertEqual(protected.read_bytes(), b"preserve production bytes\n")

    def test_missing_registration_never_generates_candidate(self):
        for optimized in (False, True):
            with self.subTest(optimized=optimized), tempfile.TemporaryDirectory() as folder:
                result, output, manifest = self.invoke(folder, omit_registration=True, optimized=optimized)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(output.exists())
                self.assertFalse(manifest.exists())

    def test_changed_registration_rejected_even_when_python_assertions_are_disabled(self):
        for optimized in (False, True):
            with self.subTest(optimized=optimized), tempfile.TemporaryDirectory() as folder:
                result, output, manifest = self.invoke(folder, registration=self.registration + b"\n", optimized=optimized)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(b"Preregistration hash changed", result.stderr)
                self.assertFalse(output.exists())
                self.assertFalse(manifest.exists())

    def test_changed_observer_appendix_rejected_even_when_prefix_is_unchanged(self):
        for optimized in (False, True):
            with self.subTest(optimized=optimized), tempfile.TemporaryDirectory() as folder:
                result, output, manifest = self.invoke(folder, observer=self.observer + '\nstrategy.entry("unexpected", strategy.long)\n', optimized=optimized)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(b"Observer appendix hash changed", result.stderr)
                self.assertFalse(output.exists())
                self.assertFalse(manifest.exists())

    def test_baseline_observer_rejects_corrupt_git_source_under_optimization(self):
        for optimized in (False, True):
            with self.subTest(optimized=optimized), tempfile.TemporaryDirectory() as folder:
                temporary = Path(folder)
                generator = temporary / "diagnostics/make_probe.py"
                generator.parent.mkdir()
                shutil.copyfile(ROOT / "diagnostics/make_probe.py", generator)
                corrupt = temporary / "corrupt.txt"
                corrupt.write_text(self.baseline + "\n// unexpected baseline bytes\n", encoding="utf-8")
                output = temporary / "result.txt"
                wrapper = temporary / "run_corrupt_source.py"
                wrapper.write_text("import pathlib,runpy,subprocess,sys\n"
                                   "generator,corrupt,output=sys.argv[1:]\n"
                                   "subprocess.check_output=lambda *a,**k:pathlib.Path(corrupt).read_bytes()\n"
                                   "sys.argv=[generator,output]\n"
                                   "runpy.run_path(generator,run_name='__main__')\n", encoding="utf-8")
                result = subprocess.run([sys.executable, "-B", *(["-O"] if optimized else []), str(wrapper), str(generator), str(corrupt), str(output)], capture_output=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
