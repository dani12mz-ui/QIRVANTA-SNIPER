"""Create an unsaved Pine Logs probe for a future Premium native parity run."""

import argparse
from pathlib import Path

from build_sniper_premium import OUTPUT, build, sha, verify_source
from observe_tradingview_final import observed_buffer


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--timeframe", choices=("5", "15", "30", "all"), required=True)
    args = parser.parse_args()
    if args.output.suffix.lower() != ".txt":
        parser.error("Choose a new .txt output, separate from the full strategy")
    source = OUTPUT.read_bytes()
    if source != build():
        raise ValueError("Premium source differs from the checked-in renderer fragments")
    verify_source(source)
    out = observed_buffer(source, None if args.timeframe == "all" else int(args.timeframe), candidate=True)
    with args.output.open("xb") as handle:
        handle.write(out)
    print(f"Source SHA-256 {sha(source)}\nObserver SHA-256 {sha(out)}")


if __name__ == "__main__":
    main()
