"""Assemble and validate a manifest for one unedited set of TradingView exports."""

import argparse
from hashlib import sha256
import json
from pathlib import Path

from research.net_campaign_evidence.h1_native import validate


HERE = Path(__file__).resolve().parent


def capture_manifest(files, output, script, calculated_bars, profile, capture_id, mode):
    output = output.resolve()
    windows = sorted(((validate.inspect_window(path), path.resolve()) for path in files),
                     key=lambda item: item[0]["first_index"])
    if not windows:
        raise ValueError("At least one original Pine Logs CSV is required")
    protocol = json.loads((HERE / "CAPTURE_PROTOCOL.json").read_bytes())
    head = windows[0][0]
    entries = []
    for facts, path in windows:
        try:
            relative = path.relative_to(output.parent).as_posix()
        except ValueError as exc:
            raise ValueError("Keep original CSV files in the manifest directory or a subdirectory") from exc
        entries.append({
            "path": relative, "sha256": facts["sha256"], "start_close_ms": facts["start_close_ms"],
            "end_close_ms": facts["end_close_ms"], "bars": facts["bars"], "events": facts["events"],
        })
    return {
        "schema": validate.SCHEMA,
        "lane": head["lane"], "symbol": head["symbol"], "frame_minutes": head["frame"],
        "chart_type": "standard", "chart_timezone": "UTC", "calculated_bars": calculated_bars,
        "first_open_ms": head["first_open_ms"], "scoring_start_ms": protocol["primary"]["score_first_close_ms"],
        "fixed_end_close_ms": protocol["primary"]["last_close_ms"],
        "source_sha256": sha256(script.read_bytes()).hexdigest(),
        "settings": {
            "core_source_commit": protocol["protected_visual_v2"], "price_feed": "standard",
            "strategy_mode": "S5", "automatic_htf": True, "core_htf": "240",
            "manual_news_pause": False, "timed_news_window": False, "gap_filter": True,
            "evidence_shield": True,
        },
        "modeled_costs": protocol["economics"]["native"],
        "provenance": {"profile": profile, "capture_id": capture_id, "mode": mode},
        "windows": entries,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True, help="Place next to the original CSV files")
    parser.add_argument("--script", type=Path, default=HERE / "H1_NATIVE_DIAGNOSTIC.pine")
    parser.add_argument("--calculated-bars", type=int, required=True)
    parser.add_argument("--profile", required=True, help="Use a local alias for the authorized test profile")
    parser.add_argument("--capture-id", required=True, help="A distinct user-assigned label for this execution")
    parser.add_argument("--mode", choices=("add", "reload", "readd"), required=True)
    parser.add_argument("files", nargs="+", type=Path)
    args = parser.parse_args()
    try:
        manifest = capture_manifest(args.files, args.out, args.script, args.calculated_bars,
                                    args.profile, args.capture_id, args.mode)
        # A failed validation must not replace a previously valid manifest.
        with args.out.with_suffix(args.out.suffix + ".checking").open("x", encoding="utf-8", newline="\n") as handle:
            scratch = Path(handle.name)
            json.dump(manifest, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        try:
            validate.validate_export(scratch)
            with args.out.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(scratch.read_text(encoding="utf-8"))
        finally:
            scratch.unlink()
    except (KeyError, ValueError, OSError) as exc:
        parser.exit(1, f"Export rejected: {exc}\n")
    print(f"Validated {manifest['lane']} capture: {len(manifest['windows'])} windows")


if __name__ == "__main__":
    main()
