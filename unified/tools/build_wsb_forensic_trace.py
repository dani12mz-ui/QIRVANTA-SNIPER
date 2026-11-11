"""Generate disposable, pinned Pine probes for the WSB investigation."""

import argparse
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]
SOURCES = {
    "s5": (
        "releases/gold-pro-rc1/QIRVANTA_GOLD_PRO_RC1_AUDIT.pine",
        "dfb154e407d32cbfc90ac957788b54cfa5b4e2034421b4b3ac26abf039d59278",
        "WSB S5",
    ),
    "rc2-audit": (
        "releases/gold-pro-rc2/QIRVANTA_GOLD_PRO_RC2_AUDIT.pine",
        "4ba04321a9ef5456e8fc61aa2b5b3ab6c44398a62b18e31b3041d9dda530d42b",
        "WSB RC2 A",
    ),
    "rc2-indicator": (
        "releases/gold-pro-rc2/QIRVANTA_GOLD_PRO_RC2.pine",
        "20551f9e06a4997257dcae035a6ec20c9bd93d998dd79c5dc6276edd47969de1",
        "WSB RC2 I",
    ),
    "wick": (
        "unified/experimental/QIRVANTA_GOLD_WICK_V3_FORMATION_STRATEGY.pine",
        "dabbb86894a6aab1bc253cbd52eb128843b5e2acbc39bbc95809979ad954d27f",
        "WSB WICK",
    ),
}
DEFAULT_START = "2026-11-11T09:00:00Z"
DEFAULT_END = "2026-11-11T16:00:00Z"
MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()


COMMON = {
    "symbol": ("q", "syminfo.tickerid"),
    "timeframe": ("q", "timeframe.period"),
    "first_open_ms": ("int", "wsbFirstOpen"),
    "open_ms": ("int", "time"),
    "close_ms": ("int", "time_close"),
    "bar_index": ("int", "bar_index"),
    "open": ("num", "open"),
    "high": ("num", "high"),
    "low": ("num", "low"),
    "close": ("num", "close"),
    "volume": ("num", "volume"),
    "volume_type": ("q", "syminfo.volumetype"),
    "mintick": ("num", "syminfo.mintick"),
    "point_value": ("num", "syminfo.pointvalue"),
    "atr_rma14": ("num", "wsbAtrRma"),
    "prior_atr_rma14": ("num", "wsbAtrRma[1]"),
    "atr_sma14": ("num", "wsbAtrSma"),
    "prior_atr_sma14": ("num", "wsbAtrSma[1]"),
}
S5 = {
    "prior_close": ("num", "close[1]"),
    "prior20_high": ("num", "tvRange20High"),
    "prior20_low": ("num", "tvRange20Low"),
    "atr_mean40": ("num", "atrMean"),
    "ema21": ("num", "emaFast"),
    "bar_range": ("num", "barRange"),
    "bearish_close_location_pass": ("bool", "bearishBar"),
    "warmup_pass": ("bool", "warmup"),
    "core_htf_ready": ("bool", "readyHTF"),
    "research_htf_ready": ("bool", "tvContextReady"),
    "h1_close_ms": ("int", "uiH1ClosedAt"),
    "h4_close_ms": ("int", "uiH4ClosedAt"),
    "h1_bias": ("int", "tvH1Bias"),
    "h4_bias": ("int", "tvH4Bias"),
    "extra_h1_required": ("bool", "tvCandidateRequireHTF"),
    "valid_atr": ("bool", "validATR"),
    "acceptable_bar": ("bool", "acceptableBar"),
    "bar_gap": ("bool", "tvTimeGap"),
    "date_pass": ("bool", "datePass"),
    "news_paused": ("bool", "newsPaused"),
    "session_pass": ("bool", "sessionPass"),
    "fvg_required": ("bool", "requireFVG"),
    "premium_discount_required": ("bool", "requirePD"),
    "structural_room_required": ("bool", "requireRoom"),
    "sell_reason": ("q", "array.get(tvReasons, 1)"),
    "buy_reason": ("q", "array.get(tvReasons, 0)"),
    "sell_phase_before": ("int", "wsbSellPhase[1]"),
    "sell_born_ms_before": ("int", "wsbSellBorn[1]"),
    "sell_level_before": ("num", "wsbSellLevel[1]"),
    "sell_anchor_before": ("num", "wsbSellAnchor[1]"),
    "sell_phase_after": ("int", "wsbSellPhase"),
    "sell_born_ms_after": ("int", "wsbSellBorn"),
    "sell_level_after": ("num", "wsbSellLevel"),
    "sell_anchor_after": ("num", "wsbSellAnchor"),
    "candidate_count": ("int", "array.size(candidates)"),
    "selected_plan_id": ("q", 'na(rcSelected) ? "" : rcSelected.id'),
    "ready_plan_id": ("q", 'rcReadyPayload == "" or na(rcBarPlan) ? "" : rcBarPlan.id'),
}
NATIVE = {
    "native_position_qty_at_close": ("num", "strategy.position_size"),
    "native_held_or_pending_plan_id": ("q", 'na(nativePlan) ? "" : nativePlan.id'),
    "native_last_status": ("q", "nativeLastStatus"),
    "native_admitted_now": ("bool", "nativeAdmittedNow"),
    "native_management_reason": ("q", "tvManagementReason"),
    "native_stop": ("num", "nativeStop"),
    "native_closed_parts": ("int", "strategy.closedtrades"),
    "native_netprofit": ("num", "strategy.netprofit"),
}
INDICATOR = {
    "decision_side": ("int", "rcCurrent.direction"),
    "decision_state_at_close": ("q", "f_rcStageText(rcCurrent.stage)"),
    "decision_known_ms": ("int", "rcCurrent.known"),
    "decision_expiry_ms": ("int", "rcCurrent.expiry"),
    "decision_reason": ("q", "rcCurrent.reason"),
    "selected_marker_side": ("int", "rcMarkerSide"),
    "selected_marker_stage": ("q", "f_rcStageText(rcMarkerStage)"),
    "chart_preset": ("q", "rcPreset"),
    "passive_markers_enabled": ("bool", "rcDisplayPassive"),
    "historical_review": ("bool", "rcReview"),
    "early_enabled": ("bool", "rcEarlyEnabled"),
    "early_side": ("int", "rcEarlySide"),
    "early_bear_geometry": ("bool", "rcEarlyBear"),
    "range_filter_enabled": ("bool", "rc2FilterActive"),
    "range_filter_blocked": ("bool", "rc2Blocked"),
    "alert_watch_enabled": ("bool", "rcAlertWatch"),
    "alert_confirmed_enabled": ("bool", "rcAlertConfirmed"),
    "alert_ready_enabled": ("bool", "rcAlertReady"),
    "alert_lifecycle_enabled": ("bool", "rcAlertLifecycle"),
    "alert_early_enabled": ("bool", "rcAlertEarly"),
}
WICK = {
    "wick_state": ("q", "wfState"),
    "wick_reason": ("q", "wfReason"),
    "wick_side": ("int", "wfStateSide"),
    "wick_prior_atr": ("num", "wfPriorAtr"),
    "wick_upper_fraction": ("num", "wfUpper"),
    "wick_lower_fraction": ("num", "wfLower"),
    "wick_buy_count": ("int", "wfBuyCount"),
    "wick_sell_count": ("int", "wfSellCount"),
    "wick_sequence_ready": ("bool", "wfSequenceReady"),
    "wick_window_high": ("num", "wfWindowHigh"),
    "wick_window_low": ("num", "wfWindowLow"),
    "wick_activity_pass": ("bool", "wfActivityPass"),
    "wick_new_formation": ("bool", "wfNewFormation"),
    "wick_breakout": ("bool", "wfBreakout"),
    "wick_retest": ("bool", "wfNewRetest"),
    "wick_false_breakout": ("bool", "wfFalseBreakout"),
    "wick_expired": ("bool", "wfExpired"),
    "wick_active_known_ms": ("int", "na(wfActive) ? na : wfActive.knownAt"),
    "wick_active_top": ("num", "na(wfActive) ? na : wfActive.top"),
    "wick_active_bottom": ("num", "na(wfActive) ? na : wfActive.bottom"),
}
WICK_SIM = {
    "wick_h1_close_ms": ("int", "wfH1Known"),
    "wick_h4_close_ms": ("int", "wfH4Known"),
    "wick_h1_side": ("int", "wfH1Side"),
    "wick_h4_side": ("int", "wfH4Side"),
    "wick_entry_policy": ("q", "wfEntryPolicy"),
    "wick_volume_policy": ("q", "wfVolumePolicy"),
    "wick_htf_required": ("bool", "wfRequireContext"),
    "wick_sim_enabled": ("bool", "wfSim"),
    "wick_sim_start_ms": ("int", "wfSimStart"),
    "wick_sim_end_ms": ("int", "wfSimEnd"),
    "wick_trade_state": ("q", "wfTradeState"),
    "wick_native_qty_at_close": ("num", "strategy.position_size"),
}


TRACE_HEADER = r'''
// Add-only closed-bar forensic journal. No orders, alerts or strategy state are changed here.
var int wsbFirstOpen = time
float wsbAtrRma = ta.atr(14)
float wsbAtrSma = ta.sma(ta.tr(true), 14)
f_wsbNum(float v) => na(v) ? "null" : str.tostring(v, "#.########")
f_wsbInt(int v) => na(v) ? "null" : str.tostring(v)
f_wsbQ(string v) =>
    '"' + str.replace_all(str.replace_all(str.replace_all(v, "\\", "\\\\"), '"', '\\"'), "\n", "\\n") + '"'
'''
TRACE_S5 = '''
TvCandidateState wsbSellWatch = array.get(tvCandidateStates, 1)
int wsbSellPhase = wsbSellWatch.phase
int wsbSellBorn = wsbSellWatch.bornTime
float wsbSellLevel = wsbSellWatch.level
float wsbSellAnchor = wsbSellWatch.anchor
'''


def utc_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError(f"Expected an explicit UTC timestamp: {value}")
    if parsed.second or parsed.microsecond:
        raise ValueError(f"Expected a whole UTC minute: {value}")
    return parsed


def pine_time(stamp: datetime) -> str:
    return f"{stamp.day:02d} {MONTHS[stamp.month - 1]} {stamp.year} {stamp:%H:%M} +0000"


def replace_exactly_one(source: str, pattern: str, replacement) -> str:
    result, count = re.subn(pattern, replacement, source, flags=re.MULTILINE)
    if count != 1:
        raise ValueError(f"Expected exactly one Pine anchor for {pattern!r}, found {count}")
    return result


def date_input(source: str, name: str, stamp: datetime) -> str:
    pattern = rf'^({re.escape(name)} = input.time\(timestamp\(")[^"]+("\))'
    return replace_exactly_one(source, pattern, lambda match: match[1] + pine_time(stamp) + match[2])


def toggle_input(source: str, name: str) -> str:
    return replace_exactly_one(source, rf"^(bool {re.escape(name)} = input.bool\()false\b", r"\g<1>true")


def trace(kind: str) -> str:
    fields = dict(COMMON)
    header = TRACE_HEADER.lstrip()
    audit_bar = "rcQaBar"
    if kind == "wick":
        fields.update(WICK)
        fields.update(WICK_SIM)
        audit_bar = "wfAuditBar"
    else:
        header += TRACE_S5
        fields.update(S5)
        if kind == "rc2-indicator":
            fields.update(INDICATOR)
            fields.update(WICK)
        else:
            fields.update(NATIVE)
            if kind == "rc2-audit":
                fields["native_lane"] = ("q", "rc2AuditLane")
                fields["range_filter_enabled"] = ("bool", "rc2FilterActive")
    lines = [header, f"if {audit_bar}", f"    string wsbRecord = '{{\"schema\":1,\"kind\":\"{kind}\"'"]
    formatter = {"num": "f_wsbNum", "int": "f_wsbInt", "q": "f_wsbQ", "bool": "str.tostring"}
    for field, (type_, expression) in fields.items():
        lines.append(f"""    wsbRecord += ',\"{field}\":' + {formatter[type_]}({expression})""")
    lines.append('    log.info("WSB_FORENSIC|" + wsbRecord + "}")')
    return "\n".join(lines) + "\n"


def build(kind: str, start: str = DEFAULT_START, end: str = DEFAULT_END, *,
          wick_sim: bool = False, cap_wick_1200: bool = False, bar_cap: int | None = None) -> str:
    if kind != "wick" and (wick_sim or cap_wick_1200):
        raise ValueError("Wick options require the wick probe")
    if cap_wick_1200 and bar_cap is not None:
        raise ValueError("Choose only one chart-cap override")
    if bar_cap is not None and bar_cap <= 0:
        raise ValueError("Chart cap must be positive")
    from_time, to_time = utc_timestamp(start), utc_timestamp(end)
    if from_time >= to_time:
        raise ValueError("Probe start must precede end")
    path, expected, title = SOURCES[kind]
    source_bytes = (ROOT / path).read_bytes()
    if sha256(source_bytes).hexdigest() != expected:
        raise ValueError(f"Pinned source hash mismatch: {path}")
    source = source_bytes.decode("utf-8")
    source = replace_exactly_one(
        source, r'^((?:strategy|indicator)\(")[^"]*(", shorttitle=")[^"]*(")',
        lambda match: f'{match[1]}{title} forensic trace{match[2]}{title}{match[3]}',
    )
    if kind == "wick":
        source = toggle_input(source, "wfAudit")
        source = date_input(source, "int wfAuditStart", from_time)
        source = date_input(source, "int wfAuditEnd", to_time)
        if wick_sim:
            source = toggle_input(source, "wfSim")
            source = date_input(source, "int wfSimStart", from_time)
            source = date_input(source, "int wfSimEnd", to_time)
        cap = 1200 if cap_wick_1200 else bar_cap
        if cap is not None:
            source = replace_exactly_one(source, r"max_lines_count=80\)", f"max_lines_count=80, calc_bars_count={cap})")
    else:
        source = toggle_input(source, "rcQa")
        source = date_input(source, "int rcQaStart", from_time)
        source = date_input(source, "int rcQaEnd", to_time)
        if bar_cap is not None:
            source = replace_exactly_one(source, r"calc_bars_count=1200(?=\))", f"calc_bars_count={bar_cap}")
    return source.rstrip() + "\n\n" + trace(kind)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=SOURCES)
    parser.add_argument("output", type=Path)
    parser.add_argument("--start", default=DEFAULT_START)
    parser.add_argument("--end", default=DEFAULT_END)
    parser.add_argument("--wick-sim", action="store_true",
                        help="enable the existing research simulator within this UTC window")
    parser.add_argument("--cap-wick-1200", action="store_true",
                        help="use the same 1200-bar chart cap as S5 and RC2")
    parser.add_argument("--bar-cap", type=int,
                        help="override chart calculation depth for a matched first-loaded-bar replay")
    args = parser.parse_args()
    try:
        data = build(args.kind, args.start, args.end, wick_sim=args.wick_sim,
                     cap_wick_1200=args.cap_wick_1200, bar_cap=args.bar_cap).encode("utf-8")
    except ValueError as exc:
        parser.error(str(exc))
    protected = [(ROOT / source).resolve() for source, _, _ in SOURCES.values()]
    if args.output.resolve() in protected:
        parser.error("output must be separate from the pinned sources")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(data)
    print(f"{args.output} sha256={sha256(data).hexdigest()} bytes={len(data)}")


if __name__ == "__main__":
    main()
