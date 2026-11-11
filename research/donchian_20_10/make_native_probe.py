"""Limit the loaded history of a disposable QA copy and add input fingerprints."""

import argparse
from pathlib import Path


SOURCE = Path(__file__).with_name("QIRVANTA_DONCHIAN_20_10_RESEARCH.pine")
PROBE = """
// Diagnostic-only. No source data dump and no change to any decision or order.
var int qaA = 0
var int qaB = 0
var int qaN = 0
if barstate.isconfirmed
    int localMonth = month(time_close - 1, "America/New_York")
    int localDay = dayofmonth(time_close - 1, "America/New_York")
    bool calendarCase = year(time_close - 1, "America/New_York") == 2026 and ((localMonth == 1 and (localDay <= 2 or localDay == 19)) or (localMonth == 2 and localDay == 16) or (localMonth == 5 and localDay == 25) or (localMonth == 6 and localDay == 19) or (localMonth == 7 and localDay == 3) or (localMonth == 9 and localDay == 7))
    if calendarCase
        log.info('D20_CALENDAR|{"index":' + str.tostring(bar_index) + ',"time":' + str.tostring(time) + ',"time_close":' + str.tostring(time_close) + '}')
    if time_close <= LAST_SIGNAL_UTC_EXCLUSIVE
        int cell = int(time / 1000) + 3 * int(time_close / 1000) + 5 * math.round(open * 10000) + 7 * math.round(high * 10000) + 11 * math.round(low * 10000) + 13 * math.round(close * 10000)
        qaA := (qaA * 65599 + cell) % 1000000007
        qaB := (qaB * 32749 + cell) % 1000000009
        qaN += 1
    if barstate.islastconfirmedhistory
        log.info('D20_HASH|{"bars":' + str.tostring(qaN) + ',"a":' + str.tostring(qaA) + ',"b":' + str.tostring(qaB) + '}')
"""


def build(count):
    if count < 101:
        raise ValueError("At least 101 loaded bars are necessary")
    source = SOURCE.read_text(encoding="utf-8")
    old = "process_orders_on_close=false, use_bar_magnifier=false)"
    if source.count(old) != 1:
        raise ValueError("Strategy declaration changed")
    # Reproduce the captures taken before the display-only correction.
    source = source.replace(
        "// A close-only strategy does not run on the first tick of the open realtime bar.\n"
        "if barstate.islastconfirmedhistory or barstate.islast", "if barstate.islast")
    source = source.replace(
        '    string frameLabel = (not supported ? timeframe.period : frame == 1440 ? "D1" : "H4") +\n'
        '         (time_close >= LAST_SIGNAL_UTC_EXCLUSIVE ? " · HISTORY" : "")',
        '    string frameLabel = not supported ? timeframe.period : frame == 1440 ? "D1" : "H4"')
    return source.replace(old, old[:-1] + f", calc_bars_count={count})") + PROBE


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("count", type=int)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    with args.output.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(build(args.count))


if __name__ == "__main__":
    main()
