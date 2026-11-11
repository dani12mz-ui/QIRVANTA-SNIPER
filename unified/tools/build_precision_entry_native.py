"""Assemble disposable Pine v6 indicator or Strategy Tester probes for Precision Entry."""

import argparse
from pathlib import Path

from build_gold_pro_rc1 import pinned
from build_gold_pro_rc2 import replace_once
from build_rc2_pressure import utc_ms


ROOT = Path(__file__).resolve().parents[2]
EXPERIMENTAL = ROOT / "unified" / "experimental"
SHADOW = EXPERIMENTAL / "direct_trend_breakout" / "shadow.pineinc"
SHADOW_SHA = "d9bf506ff86926b7664b045e6a3e863bb789a32c876aa2b78078ab59defac966"


def fragment(name):
    return (EXPERIMENTAL / "precision_entry" / f"{name}.pineinc").read_text(encoding="utf-8").rstrip() + "\n"


def build(kind, bars, start, end):
    source = pinned(SHADOW, SHADOW_SHA)
    source = replace_once(source, 'dtQa = input.bool(false,', 'dtQa = input.bool(true,')
    anchor = "f_dtGeometry(DtSetup setup, bool alreadyClaimed) =>"
    source = replace_once(source, anchor, fragment("selection") + "\n" + anchor)
    hook = '    f_dtEmit(setup, valid ? "ENTRY CANDIDATE" : "REJECTED", firstReason, geometry)'
    source = replace_once(source, hook, hook + "\n    f_peAssess(setup, geometry)")
    if kind == "tester":
        declaration = (
            'strategy("QIRVANTA Precision Entry · Timing test only", shorttitle="PE TEST", overlay=true, '
            'initial_capital=100000, currency=currency.USD, pyramiding=0, default_qty_type=strategy.fixed, '
            'default_qty_value=3, commission_type=strategy.commission.cash_per_contract, commission_value=0.175, '
            'slippage=2, calc_on_every_tick=false, calc_on_order_fills=false, process_orders_on_close=false, '
            f'use_bar_magnifier=false, margin_long=100, margin_short=100, max_bars_back=1000, calc_bars_count={bars})'
        )
        tester = fragment("tester").replace("PE_FROM_MS", str(start)).replace("PE_UNTIL_MS", str(end))
        source += "\n" + tester
    else:
        declaration = ('indicator("QIRVANTA Precision Entry · Research journal", shorttitle="PE RESEARCH", '
                       f'overlay=true, max_bars_back=1000, calc_bars_count={bars})')
        source += '\nplot(float(na), "Research only", display=display.none)\n'
    return "//@version=6\n" + declaration + "\n\n" + source


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("indicator", "tester"))
    parser.add_argument("output", type=Path)
    parser.add_argument("--bars", type=int, default=1200)
    parser.add_argument("--from-utc", type=utc_ms, default="2026-11-06T03:00:00Z")
    parser.add_argument("--until-utc", type=utc_ms, default="2026-11-11T17:30:00Z")
    args = parser.parse_args()
    if args.bars < 30 or args.from_utc >= args.until_utc:
        parser.error("Invalid history size or UTC window")
    if args.output.exists():
        parser.error("Provide a new disposable output path")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(build(args.kind, args.bars, args.from_utc, args.until_utc).encode("utf-8"))


if __name__ == "__main__":
    main()
