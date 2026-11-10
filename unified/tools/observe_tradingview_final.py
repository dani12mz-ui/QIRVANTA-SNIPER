"""Create a temporary journal buffer without the chart rendering tail."""
import argparse
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

from build_tradingview_final import CHAMPION_SHA, OUTPUT, ROOT, assemble, champion_bytes, replace_once, sha


def observed_buffer(source, minutes, *, candidate):
    with TemporaryDirectory() as directory:
        template = Path(directory) / "champion.txt"
        subprocess.run([sys.executable, "-B", str(ROOT / "unified/tools/make_ui_probe.py"),
                        str(template), "--timeframe-minutes", str(minutes or 15)],
                       cwd=ROOT, check=True, capture_output=True)
        base = champion_bytes()
        generated = template.read_bytes()
    if not generated.startswith(base):
        raise ValueError("Observation generator changed the pinned prefix")
    tail = generated[len(base):].decode("utf-8")
    if candidate:
        tail = replace_once(tail, '"ui_only|" + runModel', '"tv_final|" + tvStrategyMode + "|" + runModel')
    tail = replace_once(tail, CHAMPION_SHA, sha(source))
    tail = replace_once(tail, '    if runModel == "Всички"\n', '    if false\n')
    if minutes is None:
        tail = replace_once(tail, 'int dgCutoff = timestamp("09 Nov 2026 02:45 +0000")',
                            'int dgCutoff = timeframe.in_seconds() == 1800 ? timestamp("09 Nov 2026 02:30 +0000") : timestamp("09 Nov 2026 02:45 +0000")')
        tail = replace_once(tail, "timeframe.in_seconds() != 900",
                            "(timeframe.in_seconds() != 300 and timeframe.in_seconds() != 900 and timeframe.in_seconds() != 1800)")
    tail += '\nif barstate.isfirst and syminfo.tickerid != "OANDA:XAUUSD"\n    runtime.error("Observer requires OANDA:XAUUSD")\n'
    # TradingView rejects the full rendering tail plus journal over its compiled
    # token cap. Rendering and the final alert() dispatch follow all order logic.
    marker = b"// Presentation only: admission, orders and fills belong to the native adapter."
    if source.count(marker) != 1 or b"strategy.entry(" in source.split(marker)[1]:
        raise ValueError("Cannot identify the presentation-only tail")
    return source.split(marker)[0] + tail.encode("utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--timeframe", choices=("5", "15", "30", "all"), required=True)
    parser.add_argument("--champion", action="store_true")
    args = parser.parse_args()
    if args.output.suffix.lower() != ".txt":
        raise ValueError("Use a separate new .txt buffer")
    source = champion_bytes() if args.champion else OUTPUT.read_bytes()
    if not args.champion and source != assemble(champion_bytes()):
        raise ValueError("Build differs from checked-in source")
    result = observed_buffer(source, None if args.timeframe == "all" else int(args.timeframe),
                             candidate=not args.champion)
    with args.output.open("xb") as stream:
        stream.write(result)
    print(f"Source SHA-256 {sha(source)}\nObserver SHA-256 {sha(result)}")


if __name__ == "__main__":
    main()
