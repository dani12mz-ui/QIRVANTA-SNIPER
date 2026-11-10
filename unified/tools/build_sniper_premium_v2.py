"""Build V2 from the pinned, previously compiled V1 Pine file."""

import argparse
import re

from build_sniper_premium import CHAMPION, CHAMPION_SHA, PRESENTATION, ROOT, TRADE_WRITES, sha, split

PIN = ROOT / "unified/experimental/QIRVANTA_SNIPER_PREMIUM.pine"
PIN_SHA = "4624d103a92f6017f476e2992fd3d1c881017558d15b3e8682c377787e9ab659"
CALCULATED_BARS = 1200
OUTPUT = PIN.with_name("QIRVANTA_SNIPER_PREMIUM_V2.pine")
VIEW = tuple(ROOT / "unified/src" / f"premium_v2_{part}.pineinc" for part in ("zones", "dashboard"))
TITLE_V1 = 'strategy("QIRVANTA SNIPER PREMIUM · Experimental", shorttitle="QIRV PREMIUM"'.encode()
TITLE_V2 = 'strategy("QIRVANTA SNIPER PREMIUM V2 · Experimental", shorttitle="QIRV PREMIUM V2"'.encode()
ANCHOR_V1 = 'string panelPosition = input.string("Долу вляво",'.encode()
ANCHOR_V2 = 'string panelPosition = input.string("Горе вдясно",'.encode()
RANGE_V1 = b"max_bars_back=1000)"
RANGE_V2 = f"max_bars_back=1000, calc_bars_count={CALCULATED_BARS})".encode()


def pinned_parts():
    source = PIN.read_bytes()
    if sha(source) != PIN_SHA or sha(CHAMPION.read_bytes()) != CHAMPION_SHA:
        raise ValueError("A preserved Pine file differs from its pinned SHA-256")
    prefix, _, tail = split(source)
    # The visible chart calculation range is configurable; 0 restores all bars.
    # Different warmup datasets are not execution-parity comparisons.
    for old, new in ((TITLE_V1, TITLE_V2), (ANCHOR_V1, ANCHOR_V2), (RANGE_V1, RANGE_V2)):
        if prefix.count(old) != 1:
            raise ValueError("Expected exactly one V1 presentation declaration")
        prefix = prefix.replace(old, new, 1)
    return prefix, tail


def verify(source):
    prefix, view, tail = split(source)
    if (prefix, tail) != pinned_parts():
        raise ValueError("V2 engine or alert tail differs from V1 outside presentation and calculation range")
    if TRADE_WRITES.search(view) or re.search(rb"\brequest\.\w+\s*\(", view):
        raise ValueError("V2 presentation must not submit orders or request data")


def build():
    prefix, tail = pinned_parts()
    view = b"\n\n".join(part.read_bytes().replace(b"\r\n", b"\n").strip() for part in VIEW)
    source = prefix + PRESENTATION + view + b"\n\n" + tail
    verify(source)
    return source


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    source = build()
    if args.check:
        if OUTPUT.read_bytes() != source:
            raise SystemExit(f"Stale generated source: {OUTPUT.name}")
    else:
        OUTPUT.write_bytes(source)
    print(f"{OUTPUT.name} sha256={sha(source)} bytes={len(source)} lines={len(source.splitlines())}")


if __name__ == "__main__":
    main()
