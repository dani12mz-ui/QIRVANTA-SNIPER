"""Instrument the locally obtained, published WSB source without distributing it."""

import argparse
import hashlib
from datetime import datetime
from pathlib import Path


ORIGINAL_SHA256 = "343c0643432e3704cea4928b40c1040c92145b9efb659739466260bd099a2368"


def splice(source, needle, replacement):
    if source.count(needle) != 1:
        raise ValueError(f"Unexpected source at probe anchor {needle!r}")
    return source.replace(needle, replacement, 1)


def utc_ms(value):
    instant = datetime.fromisoformat(value)
    if instant.utcoffset() is None or instant.utcoffset().total_seconds() != 0:
        raise ValueError("Use a UTC timestamp with Z or +00:00")
    return int(instant.timestamp() * 1000)


def build(raw, cutoff, bars=1200, timeframe=5):
    normalized = raw.replace(b"\r\n", b"\n").removesuffix(b"\n")
    if hashlib.sha256(normalized).hexdigest() != ORIGINAL_SHA256:
        raise ValueError("The locally extracted WSB source SHA-256 differs from the audited snapshot")
    source = normalized.decode("utf-8")
    source = splice(
        source,
        'indicator("Wick Sequence Breakout", shorttitle="WSB", overlay = true, max_labels_count = 500, max_boxes_count = 500, max_lines_count = 500)',
        f'indicator("Wick Sequence Breakout · Forensic", shorttitle="WSB TRACE", overlay = true, max_labels_count = 500, max_boxes_count = 500, max_lines_count = 500, calc_bars_count = {bars})',
    )
    source = splice(
        source,
        "// CANDLE GEOMETRY\n",
        f"""// Read-only research capture. All decision and drawing statements are inherited.
const int wProbeEnd = {cutoff}
var int wProbeFirstOpen = time
bool wProbe = barstate.isconfirmed and syminfo.tickerid == "OANDA:XAUUSD" and timeframe.period == "{timeframe}" and chart.is_standard and time_close <= wProbeEnd
f_wpn(float x) => na(x) ? "null" : str.tostring(x, "#.##########")
f_wpi(int x) => na(x) ? "null" : str.tostring(x)
f_wpb(bool x) => x ? "true" : "false"
f_wps(string x) => '"' + x + '"'
f_wpbar() =>
    '"index":' + f_wpi(bar_index) + ',"open_ms":' + f_wpi(time) + ',"close_ms":' + f_wpi(time_close)

// CANDLE GEOMETRY
""",
    )
    source = splice(
        source,
        "    array.push(forms, f)\n",
        """    array.push(forms, f)
    if wProbe
        string created = "{" + f_wpbar() + ',"left_index":' + f_wpi(f.leftBar) +
             ',"type":' + f_wps(isMixed ? "MIXED" : bearFormed ? "BEAR" : "BULL") +
             ',"top":' + f_wpn(f.top) + ',"bottom":' + f_wpn(f.bot) +
             ',"buy_volume":' + f_wpn(f.bullVol) + ',"sell_volume":' + f_wpn(f.bearVol) +
             ',"bear_count":' + f_wpi(bearCnt) + ',"bull_count":' + f_wpi(bullCnt) + ',"any_count":' + f_wpi(anyCnt) + "}"
        log.info("WSB_ORIGINAL_FORMATION|" + created)
""",
    )
    source = splice(
        source,
        "        if removeIt\n            array.remove(forms, i)\n",
        """        if removeIt
            if wProbe
                bool broken = breakoutUp or breakoutDown
                string why = not broken ? "EXPIRED" : not (validDirUp or validDirDown) ? "VOLUME_MISMATCH" :
                     not signalSizeOk ? "SIZE_FILTER" : (validDirUp and not showBullSig) or (validDirDown and not showBearSig) ? "SIDE_DISABLED" : "SIGNAL"
                string resolution = "{" + f_wpbar() + ',"formed_index":' + f_wpi(f.formBar) +
                     ',"top":' + f_wpn(f.top) + ',"bottom":' + f_wpn(f.bot) + ',"mixed":' + f_wpb(f.mixed) +
                     ',"buy_volume":' + f_wpn(f.bullVol) + ',"sell_volume":' + f_wpn(f.bearVol) +
                     ',"break_up":' + f_wpb(breakoutUp) + ',"break_down":' + f_wpb(breakoutDown) +
                     ',"direction":' + f_wps(validDirUp ? "BUY" : validDirDown ? "SELL" : "") + ',"reason":' + f_wps(why) + "}"
                log.info("WSB_ORIGINAL_RESOLUTION|" + resolution)
            array.remove(forms, i)
""",
    )
    source = splice(
        source,
        "    tpslSL      := slP\n",
        """    if wProbe
        string plan = "{" + f_wpbar() + ',"direction":' + f_wps(isBuyTrade ? "BUY" : "SELL") +
             ',"entry":' + f_wpn(entryP) + ',"sl":' + f_wpn(slP) + ',"tp1":' + f_wpn(tp1P) +
             ',"tp2":' + f_wpn(tp2P) + ',"tp3":' + f_wpn(tp3P) + ',"risk":' + f_wpn(riskDist) + "}"
        log.info("WSB_ORIGINAL_PLAN|" + plan)
    tpslSL      := slP
""",
    )
    return source.rstrip() + "\n\n" + """var int wProbeLast = na
var bool wProbeEndEmitted = false
if wProbe
    wProbeLast := time_close
    if barstate.isfirst
        string cfg = '{"version":1,"min_count":' + f_wpi(nBars) + ',"window":' + f_wpi(windowSize) +
             ',"bear_upper":' + f_wpn(minUpperBear) + ',"bear_lower":' + f_wpn(maxLowerBear) +
             ',"bull_lower":' + f_wpn(minLowerBull) + ',"bull_upper":' + f_wpn(maxUpperBull) +
             ',"close_break":' + f_wpb(useCloseBO) + ',"wait_bars":' + f_wpi(maxWaitBars) +
             ',"range_filter":' + f_wpb(useMinRange) + ',"range_atr":' + f_wpn(minAtrMult) + ',"atr_len":' + f_wpi(atrLen) +
             ',"size_filter":' + f_wpb(useSignalSizeFilter) + ',"size_mult":' + f_wpn(signalSizeMult) + ',"size_len":' + f_wpi(signalSizeLen) +
             ',"bear_signals":' + f_wpb(showBearSig) + ',"bull_signals":' + f_wpb(showBullSig) +
             ',"absorption":' + f_wpb(useAbsorption) + ',"vol_len":' + f_wpi(volAvgLen) + ',"vol_mult":' + f_wpn(absVolMult) +
             ',"abs_max_atr":' + f_wpn(absMaxAtr) + ',"abs_close_pct":' + f_wpn(absClosePct) +
             ',"concurrent":' + f_wpi(maxConcurrent) + ',"mixed":' + f_wpb(useMixed) +
             ',"show_targets":' + f_wpb(showTargets) + ',"sl_atr":' + f_wpb(useAtrSL) + ',"tp_atr_len":' + f_wpi(tpAtrPeriod) +
             ',"sl_atr_mult":' + f_wpn(slAtrMult) + ',"sl_pct":' + f_wpn(slPercent) +
             ',"rr1":' + f_wpn(rrTP1) + ',"rr2":' + f_wpn(rrTP2) + ',"rr3":' + f_wpn(rrTP3) + "}"
        log.info("WSB_ORIGINAL_SETTINGS|" + cfg)
    string context = '{"symbol":' + f_wps(syminfo.tickerid) + ',"timeframe":' + f_wps(timeframe.period) +
         ',"first_open_ms":' + f_wpi(wProbeFirstOpen) + "," + f_wpbar() +
         ',"open":' + f_wpn(open) + ',"high":' + f_wpn(high) + ',"low":' + f_wpn(low) + ',"close":' + f_wpn(close) +
         ',"volume":' + f_wpn(volume) + ',"atr":' + f_wpn(atrVal) + ',"atr_tp":' + f_wpn(atrTP) +
         ',"avg_range_prev":' + f_wpn(avgCandleRange[1]) + ',"avg_volume":' + f_wpn(avgVol) +
         ',"upper_wick_pct":' + f_wpn(upperWickPct) + ',"lower_wick_pct":' + f_wpn(lowerWickPct) +
         ',"bear_wick":' + f_wpb(isBearWick) + ',"bull_wick":' + f_wpb(isBullWick) +
         ',"bear_abs":' + f_wpb(isBearAbs) + ',"bull_abs":' + f_wpb(isBullAbs) +
         ',"bear_count":' + f_wpi(bearCnt) + ',"bull_count":' + f_wpi(bullCnt) + ',"any_count":' + f_wpi(anyCnt) +
         ',"bear_signal":' + f_wpb(bearSignal) + ',"bull_signal":' + f_wpb(bullSignal) + "}"
    log.info("WSB_ORIGINAL_CTX|" + context)
else if barstate.isconfirmed and not na(wProbeLast) and not wProbeEndEmitted and time_close > wProbeEnd
    wProbeEndEmitted := true
    log.info("WSB_ORIGINAL_END|" + f_wpi(wProbeLast))
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("original", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--cutoff", default="2026-11-11T16:00:00Z")
    parser.add_argument("--bars", type=int, default=1200)
    parser.add_argument("--timeframe", type=int, choices=(5, 15, 30), default=5)
    args = parser.parse_args()
    if args.bars < 300:
        parser.error("--bars must retain at least 300 bars for the default formation history")
    text = build(args.original.read_bytes(), utc_ms(args.cutoff), args.bars, args.timeframe)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(text.encode("utf-8"))


if __name__ == "__main__":
    main()
