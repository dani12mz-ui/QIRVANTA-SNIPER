"""Add observations inside existing calls; never hoist/duplicate model functions.

Input must be the exact baseline plus root's observation-only appendix. Output is
a NEW .txt editor buffer. All original characters, callsites and statement order
are retained. Removing our four insertions restores the input byte for byte.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re

BASE_SHA256 = "c6bfe1af0604c447eddaed1db5195bfe2c07d3826e7fafc70113235e9f25f1a5"
OBSERVER_MARKER = "// DIAGNOSTIC OBSERVER ONLY. Baseline above is byte-identical to commit 48e5cdd."
FAST_ANCHOR = "f_fast(int d, array<Plan> out, array<int> lastBars, array<Event> ev) =>"
FAST_LINE = "    bool local = d == 1 ? close > emaFast and emaFast > emaFast[1] and bullMomentum and rsi >= 53 : close < emaFast and emaFast < emaFast[1] and bearMomentum and rsi <= 47\n"
REVERSAL_LINE = "    bool risingRsi = d == 1 ? rsi > rsi[1] and rsi >= 33 : rsi < rsi[1] and rsi <= 67\n"


def _baseline_prefix(text: str) -> str:
    marker = text.index(OBSERVER_MARKER)
    prefix = text[:marker]
    # Root inserts a leading newline before its marker. Preserve the complete
    # input; strip at most the appendix's extra newline to verify baseline hash.
    for candidate in (prefix, prefix[:-1], prefix[:-2]):
        if hashlib.sha256(candidate.encode("utf-8")).hexdigest() == BASE_SHA256:
            return candidate
    raise ValueError("Input is not the exact approved standalone plus an observer appendix")


def instrument(text: str) -> tuple[str, dict]:
    baseline = _baseline_prefix(text)
    cutoff = re.search(r'^int dgCutoff = (.+)$', text[len(baseline):], re.MULTILINE)
    if not cutoff:
        raise ValueError("Missing observer dgCutoff declaration")
    for anchor in (FAST_ANCHOR, FAST_LINE, REVERSAL_LINE):
        if text.count(anchor) != 1:
            raise ValueError("Missing or repeated baseline injection anchor: " + anchor[:50])
    prelude = '''// WARNING PROBE BEGIN — observation-only globals, no trading mutations.
int dgWarningCutoff = __CUTOFF__
float dgExpectedClosePrev = close[1]
float dgExpectedEmaPrev = emaFast[1]
float dgExpectedRsiPrev = rsi[1]
bool dgExpectedFastCrossLong = not na(fastBreakHigh) and close > fastBreakHigh and close[1] <= fastBreakHigh
bool dgExpectedFastCrossShort = not na(fastBreakLow) and close < fastBreakLow and close[1] >= fastBreakLow
bool dgExpectedFastLocalLong = close > emaFast and emaFast > emaFast[1] and bullMomentum and rsi >= 53
bool dgExpectedFastLocalShort = close < emaFast and emaFast < emaFast[1] and bearMomentum and rsi <= 47
bool dgExpectedReversalRsiLong = rsi > rsi[1] and rsi >= 33
bool dgExpectedReversalRsiShort = rsi < rsi[1] and rsi <= 67
var array<int> dgWarningCalls = array.new<int>(4, 0)
var array<int> dgWarningMismatches = array.new<int>(4, 0)
var array<string> dgWarningDetails = array.new<string>()
f_dgSameHistory(float a, float b) => (na(a) and na(b)) or (not na(a) and not na(b) and a == b)
// WARNING PROBE END GLOBALS
'''.replace("__CUTOFF__", cutoff.group(1))
    fast = '''    // WARNING PROBE BEGIN FAST — does not call f_fast or change its result.
    if time_close <= dgWarningCutoff
        int dgSlot = d == 1 ? 0 : 1
        bool dgOK = f_dgSameHistory(close[1], dgExpectedClosePrev) and f_dgSameHistory(emaFast[1], dgExpectedEmaPrev) and crossed == (d == 1 ? dgExpectedFastCrossLong : dgExpectedFastCrossShort) and local == (d == 1 ? dgExpectedFastLocalLong : dgExpectedFastLocalShort)
        array.set(dgWarningCalls, dgSlot, array.get(dgWarningCalls, dgSlot) + 1)
        if not dgOK
            array.set(dgWarningMismatches, dgSlot, array.get(dgWarningMismatches, dgSlot) + 1)
            if array.size(dgWarningDetails) < 20
                array.push(dgWarningDetails, "fast|" + str.tostring(d) + "|" + str.tostring(time_close) + "|closePrev=" + f_price(close[1]) + "|expectedClose=" + f_price(dgExpectedClosePrev) + "|emaPrev=" + f_price(emaFast[1]) + "|expectedEma=" + f_price(dgExpectedEmaPrev))
    // WARNING PROBE END FAST
'''
    reversal = '''    // WARNING PROBE BEGIN REVERSAL — does not call f_reversal or change its result.
    if time_close <= dgWarningCutoff
        int dgSlot = d == 1 ? 2 : 3
        bool dgOK = f_dgSameHistory(rsi[1], dgExpectedRsiPrev) and risingRsi == (d == 1 ? dgExpectedReversalRsiLong : dgExpectedReversalRsiShort)
        array.set(dgWarningCalls, dgSlot, array.get(dgWarningCalls, dgSlot) + 1)
        if not dgOK
            array.set(dgWarningMismatches, dgSlot, array.get(dgWarningMismatches, dgSlot) + 1)
            if array.size(dgWarningDetails) < 20
                array.push(dgWarningDetails, "reversal|" + str.tostring(d) + "|" + str.tostring(time_close) + "|rsiPrev=" + str.tostring(rsi[1]) + "|expectedRsi=" + str.tostring(dgExpectedRsiPrev))
    // WARNING PROBE END REVERSAL
'''
    tail = '''
// WARNING PROBE BEGIN SUMMARY — runs after the intact native observer appendix.
if barstate.isconfirmed and time_close == dgWarningCutoff
    string dgWarningRow = '{"schema":"qirvanta.warnings.v1","kind":"history_scope","baseline_sha256":"__BASE_SHA__","symbol":' + f_q(syminfo.tickerid) + ',"timeframe":' + f_q(timeframe.period) + ',"cutoff":' + f_int(dgWarningCutoff) + ',"confirmed_bar_count":' + f_int(dgCount) + ',"fast_long_calls":' + f_int(array.get(dgWarningCalls, 0)) + ',"fast_short_calls":' + f_int(array.get(dgWarningCalls, 1)) + ',"reversal_long_calls":' + f_int(array.get(dgWarningCalls, 2)) + ',"reversal_short_calls":' + f_int(array.get(dgWarningCalls, 3)) + ',"fast_long_mismatches":' + f_int(array.get(dgWarningMismatches, 0)) + ',"fast_short_mismatches":' + f_int(array.get(dgWarningMismatches, 1)) + ',"reversal_long_mismatches":' + f_int(array.get(dgWarningMismatches, 2)) + ',"reversal_short_mismatches":' + f_int(array.get(dgWarningMismatches, 3)) + ',"netprofit":' + f_num(strategy.netprofit) + ',"closed_count":' + f_int(strategy.closedtrades) + ',"scope":"DIRECT_GLOBAL_HISTORY_AND_PREDICATES_NOT_FULL_SIGNAL_PARITY"}'
    log.info("QIRVANTA_WARNING_PROBE " + dgWarningRow)
    for dgDetail in dgWarningDetails
        log.info("QIRVANTA_WARNING_MISMATCH " + dgDetail)
// WARNING PROBE END SUMMARY
'''.replace("__BASE_SHA__", BASE_SHA256)
    output = text.replace(FAST_ANCHOR, prelude + FAST_ANCHOR)
    output = output.replace(FAST_LINE, FAST_LINE + fast)
    output = output.replace(REVERSAL_LINE, REVERSAL_LINE + reversal) + tail
    restored = output
    for added in (prelude, fast, reversal, tail):
        if restored.count(added) != 1:
            raise AssertionError("Insertion uniqueness lost")
        restored = restored.replace(added, "", 1)
    if restored != text:
        raise AssertionError("Instrumentation changed existing source")
    # No original model call/order call was moved, removed or duplicated.
    def executable_calls(value: str) -> list[str]:
        value = re.sub(r"//[^\n]*", "", value)
        return re.findall(r"\b(?:f_fast|f_reversal|strategy\.(?:entry|exit|close|cancel))\s*\(", value)
    if executable_calls(output) != executable_calls(text):
        raise AssertionError("A protected callsite changed")
    original_appendix = text[len(baseline):]
    if original_appendix not in output:
        raise AssertionError("Native observer appendix changed")
    manifest = {"baseline_sha256": BASE_SHA256, "input_sha256": hashlib.sha256(text.encode()).hexdigest(), "output_sha256": hashlib.sha256(output.encode()).hexdigest(), "native_appendix_sha256": hashlib.sha256(original_appendix.encode()).hexdigest(), "native_appendix_unchanged": True, "remove_insertions_restores_input": True, "original_model_and_order_calls_unchanged": True, "insertions": 4, "probe_run_status": "UNVERIFIED", "expected_result": "Each of four call counts equals confirmed_bar_count; four mismatches are zero", "scope": "History reads and pure predicates inside original calls; no full signal parity or profitability assertion"}
    return output, manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    output_path = args.output.resolve()
    own_directory = Path(__file__).resolve().parent
    if own_directory not in output_path.parents or output_path.suffix != ".txt":
        parser.error("Output must be a new .txt under diagnostics/warnings/")
    manifest_path = output_path.with_suffix(".manifest.json")
    if output_path.exists() or manifest_path.exists():
        parser.error("Refusing to overwrite an existing output or manifest")
    source = args.input.read_bytes().decode("utf-8")
    output, manifest = instrument(source)
    output_path.write_text(output, encoding="utf-8", newline="\n")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"output": str(output_path), **manifest}, ensure_ascii=False))


if __name__ == "__main__":
    main()
