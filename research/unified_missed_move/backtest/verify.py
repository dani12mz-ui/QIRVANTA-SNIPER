"""Reproduce the pinned Direct Trend and Precision Entry retrospective results."""

import argparse
from gzip import decompress
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import ModuleType


ROOT = Path(__file__).resolve().parents[3]
DIRECT_REF = "bcda58bedf20d6dd626e49849b48c613746167f1"
PRECISION_REF = "a0666824a8cc6ad9b0055c47ca74292d3b8d3d90"
DIRECT_EVIDENCE = "research/direct_trend_breakout/evidence"
PRECISION_EVIDENCE = "research/precision_entry/evidence"
REGISTRATION = "research/precision_entry/evaluation_registration.json"
MODELS = (
    "S5_RAW", "RC2", "RC2_PLUS_ORIGINAL_SHADOW", "STRUCTURAL_BREAK", "RETEST_CONTINUATION",
    "CONSOLIDATION_BREAK", "PRECISION_POOL", "RC2_PLUS_PRECISION", "RC2_PLUS_PRECISION_FIXED_BASE_OFFERS",
)
sys.path.insert(0, str(ROOT))
from gold_pro import direct_breakout_research as direct


def blob(commit, path):
    return subprocess.check_output(["git", "show", f"{commit}:{path}"], cwd=ROOT)


def digest(data):
    return sha256(data).hexdigest()


def native_json(value):
    return json.loads(json.dumps(value, allow_nan=False, default=str))


def exact(report, reference, name):
    if native_json(report) != reference:
        raise ValueError(f"Recomputed {name} differs from the pinned archive")


def load_precision(name, raw):
    module = ModuleType(name)
    module.__file__ = f"{PRECISION_REF}:{name.replace('.', '/')}.py"
    sys.modules[name] = module
    exec(compile(raw, module.__file__, "exec"), module.__dict__)
    return module


def precision_reports(original_summary):
    with TemporaryDirectory() as directory:
        temporary = Path(directory)
        for path, expected in original_summary["source_sha256"].items():
            raw = blob(PRECISION_REF, path)
            if digest(raw) != expected:
                raise ValueError(f"Changed Precision input: {path}")
            target = temporary / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            if path in ("gold_pro/direct_trend_breakout.py", "gold_pro/direct_breakout_research.py"):
                if (ROOT / path).read_bytes() != raw:
                    raise ValueError(f"Shared execution source changed: {path}")
        registration = temporary / REGISTRATION
        registration.write_bytes(blob(PRECISION_REF, REGISTRATION))
        if digest(registration.read_bytes()) != original_summary["registration_sha256"]:
            raise ValueError("Changed Precision registration")
        load_precision("gold_pro.precision_entry", (temporary / "gold_pro/precision_entry.py").read_bytes())
        precision = load_precision("gold_pro.precision_entry_research",
                                   (temporary / "gold_pro/precision_entry_research.py").read_bytes())
        precision.ROOT = temporary
        precision.REGISTRATION = registration
        diagnostic = load_precision("gold_pro.precision_entry_diagnostic",
                                    blob(PRECISION_REF, "gold_pro/precision_entry_diagnostic.py"))
        report = precision.run()
        return report, precision.compact(report), diagnostic.run()


def precision_panel(panel):
    result = {}
    for name, scenario in panel["cost_scenarios"].items():
        result[name] = {
            "costs": scenario["costs"],
            "origin_retest_pairs": scenario["origin_retest_pairs"],
            "displacement": scenario["displacement"],
            "models": {
                model: {"metrics": lane["windows"]["full"], "states": lane["states"],
                        "recall": lane["recall"], "outside_same_side_event": lane["outside_same_side_event"]}
                for model, lane in scenario["lanes"].items() if model in MODELS
            },
        }
    return {"independent_hours": panel["independent_hours"], "cost_scenarios": result}


def diagnostic_panel(panel):
    return {key: panel[key] for key in ("scored_window_utc", "attribution", "combined_effect_usd",
                                       "admitted_shadow_geometry_by_outcome", "shadow_loss_descriptions")}


def verify():
    direct_reference = blob(DIRECT_REF, f"{DIRECT_EVIDENCE}/historical_replay.json.gz")
    precision_reference = blob(PRECISION_REF, f"{PRECISION_EVIDENCE}/precision_replay.json.gz")
    precision_summary = blob(PRECISION_REF, f"{PRECISION_EVIDENCE}/precision_summary.json")
    diagnostic_reference = blob(PRECISION_REF, f"{PRECISION_EVIDENCE}/diagnostic.json")
    precision_report, compact, diagnostic = precision_reports(json.loads(precision_summary))
    exact(precision_report, json.loads(decompress(precision_reference)), "Precision complete replay")
    exact(compact, json.loads(precision_summary), "Precision summary")
    exact(diagnostic, json.loads(diagnostic_reference), "Shadow loss diagnostic")
    direct_report = direct.run()
    exact(direct_report, json.loads(decompress(direct_reference)), "Direct Trend complete replay")
    return {
        "status": "RETROSPECTIVE_REPRODUCED_RESEARCH_ONLY",
        "matched_complete_records": {
            "direct_trend": {"commit": DIRECT_REF, "sha256": digest(direct_reference)},
            "precision_entry": {"commit": PRECISION_REF, "sha256": digest(precision_reference)},
            "precision_summary": {"commit": PRECISION_REF, "sha256": digest(precision_summary)},
            "shadow_loss_diagnostic": {"commit": PRECISION_REF, "sha256": digest(diagnostic_reference)},
        },
        "historical_registration": compact["registration"]["historical"],
        "simulation": compact["registration"]["simulation"],
        "prospective_registration": compact["registration"]["prospective"],
        "cross_frame": compact["cross_frame"],
        "matched_precision_results": {name: precision_panel(panel) for name, panel in compact["frames"].items()},
        "original_shadow_loss_attribution": {
            name: diagnostic_panel(panel) for name, panel in diagnostic["historical_matched_windows"].items()
        },
        "direct_trend_input_sha256": direct_report["inputs_sha256"],
        "promotion_status": compact["promotion_status"],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = verify()
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
