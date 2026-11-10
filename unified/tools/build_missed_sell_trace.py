"""Build a temporary TradingView observer for the November 10 S5 event."""

import argparse
import hashlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "unified/experimental/QIRVANTA_SNIPER_PREMIUM_V2.pine"
BASE_SHA = "a920f15370b536f19def0205b21915e1a3d2e4ed4c76c80bd22f5e8aa6bf7c10"
PRESENTATION = "\n// Presentation only: admission, orders and fills belong to the native adapter."
TRACE = r"""
// Bar-close evidence only. Never submit, cancel or change an order here.
int traceBegin = timestamp("09 Nov 2026 18:00 +0000")
int traceEnd = timestamp("10 Nov 2026 15:30 +0000")
if barstate.isfirst and (syminfo.tickerid != "OANDA:XAUUSD" or not chart.is_standard or tvStrategyMode != "S5" or (tfSeconds != 300 and tfSeconds != 900 and tfSeconds != 1800))
    runtime.error("Trace requires S5, OANDA:XAUUSD, standard candles, and M5/M15/M30")
if processBar and time >= traceBegin and time_close <= traceEnd
    TvCandidateState traceBuy = array.get(tvCandidateStates, 0)
    TvCandidateState traceSell = array.get(tvCandidateStates, 1)
    string trace = "[" + f_int(time) + "," + f_int(time_close) + "," + f_int(bar_index)
    for v in array.from(open, high, low, close, volume, atr, atrMean, barRange, emaFast, emaSlow, rsi, adx, efficiency, tvRange20High, tvRange20Low, tvAtrPercentile, strategy.position_size, nativeStop)
        trace += "," + f_num(v)
    for b in array.from(warmup, tvEntryFrame, tvContextReady, readyHTF, validATR, acceptableBar, tvTimeGap, datePass, newsPaused, sessionPass, tvCandidateRequireHTF, requireFVG, requirePD)
        trace += "," + str.tostring(b)
    trace += "," + f_int(tvH1Bias) + "," + f_int(tvH4Bias) + "," + f_q(nativePortfolio)
    trace += "," + f_q(array.get(tvReasons, 0)) + "," + f_q(array.get(tvReasons, 1))
    trace += "," + f_int(traceBuy.phase) + "," + f_int(traceBuy.bornTime) + "," + f_num(traceBuy.level) + "," + f_num(traceBuy.anchor)
    trace += "," + f_int(traceSell.phase) + "," + f_int(traceSell.bornTime) + "," + f_num(traceSell.level) + "," + f_num(traceSell.anchor)
    trace += "," + f_q(na(selectedNativePlan) ? "" : selectedNativePlan.id) + "," + f_q(na(nativePlan) ? "" : nativePlan.id)
    trace += "," + f_q(nativeLastStatus) + "," + f_int(strategy.closedtrades) + "," + f_num(strategy.netprofit)
    log.info("S5_TRACE|" + timeframe.period + "|" + trace + "]")
    for e in events
        if e.book == "CORE" or e.book == "NATIVE"
            string traceEvent = "[" + f_int(time_close) + "," + f_int(e.effectiveTime) + "," + f_q(e.kind) + "," + f_q(e.book) + "," + f_q(e.planId) + "," + f_int(e.side)
            traceEvent += "," + f_q(e.reason) + "," + f_num(e.price) + "," + f_num(e.reference) + "," + f_num(e.stop) + "," + f_num(e.tp1) + "," + f_num(e.tp2) + "," + f_num(e.tp3)
            traceEvent += "," + f_num(e.quantityFraction) + "," + f_num(e.cashPnl) + "," + f_num(e.netR)
            log.info("S5_EVENT|" + timeframe.period + "|" + traceEvent + "]")
"""


def build(bars: int) -> str:
    original = SOURCE.read_bytes()
    if hashlib.sha256(original).hexdigest() != BASE_SHA:
        raise ValueError("S5 baseline SHA-256 differs from commit 3d5f3c1")
    source = original.decode("utf-8")
    prefix, separator, _ = source.partition(PRESENTATION)
    if not separator:
        raise ValueError("S5 presentation boundary missing")
    prefix = prefix.replace('string tvStrategyMode = input.string("S0",', 'string tvStrategyMode = input.string("S5",', 1)
    prefix = prefix.replace('shorttitle="QIRV PREMIUM V2"', 'shorttitle="S5 TRACE"', 1)
    if bars != 1200:
        prefix = prefix.replace("calc_bars_count=1200", f"calc_bars_count={bars}", 1)
    return prefix.rstrip() + "\n\n" + TRACE.lstrip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--bars", type=int, choices=(0, 700, 1200, 1800, 2200), default=1200)
    args = parser.parse_args()
    payload = build(args.bars).encode("utf-8")
    args.output.write_bytes(payload)
    print(f"{args.output} sha256={hashlib.sha256(payload).hexdigest()} bytes={len(payload)}")


if __name__ == "__main__":
    main()
