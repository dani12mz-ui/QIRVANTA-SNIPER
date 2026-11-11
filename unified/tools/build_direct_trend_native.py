"""Create disposable native forensic probes from the pinned Pressure and RC2 Audit scripts."""

import argparse
from pathlib import Path

from build_gold_pro_rc1 import pinned
from build_gold_pro_rc2 import replace_once
from build_rc2_pressure import (
    AUDIT_NAME, BASES, FEATURE_NAME, FEATURE_SHORT, OUTPUT, RELEASE, qa_script, rename, utc_ms,
)


PRESSURE_SHA = "e27f5a5adc00124d085ebca6d0d6372ef9e099708ff93edcbd7bcf312841f0c4"
ROOT = Path(__file__).resolve().parents[2]
SHADOW = ROOT / "unified" / "experimental" / "direct_trend_breakout" / "shadow.pineinc"
EXPERIMENT = ROOT / "research" / "direct_trend_breakout" / "QIRVANTA_GOLD_PRO_DIRECT_TREND_SHADOW.pine"
DISPLAY_ANCHOR = "// Presentation reads the frozen decision and observation streams."
REVIEW_ROW = """// The optional line describes only the latest shadow event at the selected close.
var string dtReviewState = ""
var string dtReviewReason = ""
var int dtReviewSide = 0
var int dtReviewKnownMs = na
if rcReview and barstate.isconfirmed and time_close == rcReviewAt
    dtReviewState := dtShadowState
    dtReviewReason := dtShadowReason
    dtReviewSide := dtShadowSide
    dtReviewKnownMs := dtShadowKnownMs
"""
DISPLAY_ROW = """    if dtShow
        string dtStatus = rcReview ? dtReviewState : dtShadowState
        string dtReason = rcReview ? dtReviewReason : dtShadowReason
        int dtSide = rcReview ? dtReviewSide : dtShadowSide
        int dtKnown = rcReview ? dtReviewKnownMs : dtShadowKnownMs
        string dtSideText = dtSide > 0 ? "BUY " : dtSide < 0 ? "SELL " : ""
        string dtCaption = na(dtKnown) ? "DT SHADOW · —" :
             "DT SHADOW · " + dtSideText + dtStatus + " · " + str.format_time(dtKnown, "HH:mm", "UTC")
        f_rcViewCell(0, 26, "  " + f_rcViewClip(dtCaption, rcViewMobile ? 36 : 58),
             rcViewGold, rcViewGraphite, rcViewSmall, text.align_left, false,
             "Latest diagnostic event · " + f_rcViewUtc(dtKnown) + "\\n" + dtReason)
"""
BASE_FIELDS = {
    "symbol": "f_q(syminfo.tickerid)",
    "frame": "f_q(timeframe.period)",
    "first_open_ms": "f_int(rcFirstOpen)",
    "open_ms": "f_int(time)",
    "known_ms": "f_int(time_close)",
    "open": "f_num(open)",
    "high": "f_num(high)",
    "low": "f_num(low)",
    "close": "f_num(close)",
    "volume": "f_num(volume)",
    "atr_previous": "f_num(atr[1])",
    "pivot_low": "f_num(lastPivotLow)",
    "previous_pivot_low": "f_num(prevPivotLow)",
    "pivot_high": "f_num(lastPivotHigh)",
    "previous_pivot_high": "f_num(prevPivotHigh)",
    "new_pivot_low": "f_num(pivotLow)",
    "new_pivot_high": "f_num(pivotHigh)",
    "prior20_high": "f_num(tvRange20High)",
    "prior20_low": "f_num(tvRange20Low)",
    "h1_bias": "f_int(tvH1Bias)",
    "h4_bias": "f_int(tvH4Bias)",
    "h1_known_ms": "f_int(uiH1ClosedAt)",
    "h4_known_ms": "f_int(uiH4ClosedAt)",
    "warmup": "str.tostring(warmup)",
    "htf_gate_required": "str.tostring(tvCandidateRequireHTF)",
    "gap": "str.tostring(tvTimeGap)",
    "range_pass": "str.tostring(acceptableBar)",
    "atr_pass": "str.tostring(validATR)",
    "session_pass": "str.tostring(sessionPass)",
    "date_pass": "str.tostring(datePass)",
    "news_paused": "str.tostring(newsPaused)",
    "buy_phase_before": "f_int(dtBuyPhase[1])",
    "sell_phase_before": "f_int(dtSellPhase[1])",
    "buy_phase": "f_int(dtBuyPhase)",
    "sell_phase": "f_int(dtSellPhase)",
    "buy_reason": "f_q(array.get(tvReasons, 0))",
    "sell_reason": "f_q(array.get(tvReasons, 1))",
    "candidate_count": "f_int(array.size(candidates))",
    "ready_plan": 'f_q(rcReadyPayload == "" or na(rcBarPlan) ? "" : rcBarPlan.id)',
    "range_filter_enabled": "str.tostring(rc2FilterActive)",
}
INDICATOR_FIELDS = {
    "early_enabled": "str.tostring(rcEarlyEnabled)",
    "early_side": "f_int(rcEarlySide)",
    "early_bear_geometry": "str.tostring(rcEarlyBear)",
    "early_bull_geometry": "str.tostring(rcEarlyBull)",
    "early_previous_atr_expansion": "str.tostring(rcEarlyExpansion)",
    "early_prior_high": "f_num(rcEarlyHigh)",
    "early_prior_low": "f_num(rcEarlyLow)",
    "early_alert_enabled": "str.tostring(rcAlertEarly)",
    "market_bias": "f_int(rcCurrent.bias)",
    "m15_trend": "f_int(rcM15ClosedTrend)",
    "decision": "f_q(f_rcStageText(rcCurrent.stage))",
    "decision_side": "f_int(rcCurrent.direction)",
    "decision_known_ms": "f_int(rcCurrent.known)",
    "decision_reason": "f_q(rcCurrent.reason)",
}
AUDIT_FIELDS = {
    "audit_lane": "f_q(rc2AuditLane)",
    "native_position": "f_num(strategy.position_size)",
    "native_pending_or_held": 'f_q(na(nativePlan) ? "" : nativePlan.id)',
    "native_admitted": "str.tostring(nativeAdmittedNow)",
}


def trace(kind):
    fields = BASE_FIELDS | (INDICATOR_FIELDS if kind == "indicator" else AUDIT_FIELDS)
    lines = [
        "// Forensic output at confirmed closes, with frozen base decisions.",
        "TvCandidateState dtBuy = array.get(tvCandidateStates, 0)",
        "TvCandidateState dtSell = array.get(tvCandidateStates, 1)",
        "int dtBuyPhase = dtBuy.phase",
        "int dtSellPhase = dtSell.phase",
        "if rcQaBar",
        f"""    string dtJson = '{{"kind":"{kind}"'""",
    ]
    lines.extend(f"""    dtJson += ',"{name}":' + {expression}""" for name, expression in fields.items())
    lines.append('    log.info("DT_BASE|" + dtJson + "}")')
    return "\n".join(lines) + "\n"


def build(kind, start, end, bars, early=False, s5=False, review=None):
    if kind == "indicator":
        source = pinned(OUTPUT, PRESSURE_SHA)
        source = rename(source, FEATURE_NAME, FEATURE_SHORT, "Direct Trend Breakout · RC2 indicator trace", "DT RC2 I")
        if early:
            source = replace_once(source, 'rcEarlyEnabled = input.bool(false,',
                                  'rcEarlyEnabled = input.bool(true,')
        if review is not None:
            source = replace_once(source, 'rcPreset = input.string("PREMIUM",',
                                  'rcPreset = input.string("FULL AUDIT",')
            source = replace_once(source, 'rcReviewEnabled = input.bool(false,',
                                  'rcReviewEnabled = input.bool(true,')
            source = replace_once(source, 'rcReviewAt = input.time(timestamp("11 Nov 2026 05:40 +0000"),',
                                  f'rcReviewAt = input.time({review},')
    else:
        name, digest = BASES[1]
        source = rename(pinned(RELEASE / name, digest), AUDIT_NAME, "RC2 AUDIT",
                        "Direct Trend Breakout · RC2 audit trace", "DT RC2 A")
    return qa_script(source, start, end, bars, s5).rstrip() + "\n\n" + trace(kind)


def add_shadow(source):
    snippet = SHADOW.read_text(encoding="utf-8").rstrip()
    source = replace_once(source, DISPLAY_ANCHOR, snippet + "\n\n" + REVIEW_ROW + "\n" + DISPLAY_ANCHOR)
    source = replace_once(source, "table.new(rcViewPosition, 12, 26,",
                          "table.new(rcViewPosition, 12, dtShow ? 27 : 26,")
    source = replace_once(source, "    if rcPressureShow\n        f_rcPressureWidgetLayout()",
                          "    if rcPressureShow\n        f_rcPressureWidgetLayout()\n"
                          "    if dtShow\n        table.merge_cells(rcViewTable, 0, 26, 11, 26)")
    return replace_once(source, '\nbool rcAlertWatch = input.bool', DISPLAY_ROW + '\nbool rcAlertWatch = input.bool')


def build_shadow_probe(start, end, bars, early=False, review=None, enabled=False):
    source = build("indicator", start, end, bars, early=early, review=review)
    source = rename(source, "Direct Trend Breakout · RC2 indicator trace", "DT RC2 I",
                    "QIRVANTA GOLD PRO · Direct Trend Shadow QA", "DT SHADOW")
    source = add_shadow(source)
    if enabled:
        for name in ("dtShow", "dtQa"):
            source = replace_once(source, f"{name} = input.bool(false,", f"{name} = input.bool(true,")
    return source


def shadow_indicator():
    source = rename(pinned(OUTPUT, PRESSURE_SHA), FEATURE_NAME, FEATURE_SHORT,
                    "QIRVANTA GOLD PRO · Direct Trend Shadow", "DT SHADOW")
    return add_shadow(source)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("indicator", "audit", "shadow-probe", "shadow-indicator"))
    parser.add_argument("output", type=Path, nargs="?")
    parser.add_argument("--start", type=utc_ms, default="2026-11-11T00:00:00Z")
    parser.add_argument("--end", type=utc_ms, default="2026-11-11T17:30:00Z")
    parser.add_argument("--bars", type=int, default=1200)
    parser.add_argument("--early", action="store_true")
    parser.add_argument("--s5", action="store_true")
    parser.add_argument("--review", type=utc_ms)
    parser.add_argument("--enable-shadow", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.bars < 1 or args.start >= args.end:
        parser.error("Invalid log range or bar limit")
    if (args.kind == "audit" and (args.early or args.review is not None)) or (args.kind != "audit" and args.s5):
        parser.error("EARLY and historical review require indicator. S5 RAW requires audit.")
    if args.kind == "shadow-indicator":
        if args.output is not None:
            parser.error("Shadow indicator has a fixed research path")
        expected = shadow_indicator().encode("utf-8")
        if args.check:
            if not EXPERIMENT.exists() or EXPERIMENT.read_bytes() != expected:
                parser.error("Generated shadow indicator differs")
        else:
            EXPERIMENT.write_bytes(expected)
        return
    if args.output is None or args.check:
        parser.error("Provide a new output path for a native probe")
    if args.output.exists():
        parser.error("Use a new output path for the disposable probe")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.kind == "shadow-probe":
        source = build_shadow_probe(args.start, args.end, args.bars, args.early, args.review, args.enable_shadow)
    else:
        source = build(args.kind, args.start, args.end, args.bars, args.early, args.s5, args.review)
    args.output.write_bytes(source.encode("utf-8"))


if __name__ == "__main__":
    main()
