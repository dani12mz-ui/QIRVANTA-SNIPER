"""Build the presentation-only Direct Trend Visibility indicator and native QA probes."""

import argparse
from pathlib import Path

from build_gold_pro_rc1 import pinned
from build_gold_pro_rc2 import replace_once
from build_rc2_pressure import qa_script, rename, utc_ms


ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / "research/direct_trend_breakout/QIRVANTA_GOLD_PRO_DIRECT_TREND_SHADOW.pine"
BASE_SHA = "275332fdaf66fefb286bb1d2902eec4cf03792af9b4e4368c79e9541cd5f79bf"
PARTS = ROOT / "unified/experimental/direct_trend_visibility"
OUTPUT = ROOT / "research/direct_trend_visibility/QIRVANTA_GOLD_PRO_SELL_VISIBILITY.pine"
BASE_NAME = "QIRVANTA GOLD PRO · Direct Trend Shadow"
BASE_SHORT = "DT SHADOW"
NAME = "QIRVANTA GOLD PRO · Directional Visibility"
SHORT = "DTV RC2"
DISPLAY = "// Presentation reads the frozen decision and observation streams."
OLD_SHADOW_REVIEW = "// The optional line describes only the latest shadow event at the selected close."
ALERTS = "bool rcAlertWatch = input.bool"


def part(name):
    return (PARTS / f"{name}.pineinc").read_text(encoding="utf-8").rstrip() + "\n"


def remove_old_shadow_presentation(source):
    before, tail = source.split(OLD_SHADOW_REVIEW, 1)
    _, display = tail.split(DISPLAY, 1)
    display, alerts = display.split(ALERTS, 1)
    display = replace_once(display, "table.new(rcViewPosition, 12, dtShow ? 27 : 26,",
                           "table.new(rcViewPosition, 12, 26,")
    display = replace_once(display, "    if dtShow\n        table.merge_cells(rcViewTable, 0, 26, 11, 26)\n", "")
    display = display.split("    if dtShow\n        string dtStatus", 1)[0]
    return before, DISPLAY + display, ALERTS + alerts


def build(base):
    # The original alert block is carried byte for byte after the display changes.
    source = rename(base, BASE_NAME, BASE_SHORT, NAME, SHORT)
    before, display, alerts = remove_old_shadow_presentation(source)
    marker = "// DIRECTIONAL VISIBILITY SNAPSHOT"
    lifecycle, snapshot = part("lifecycle").split(marker, 1)
    before = replace_once(before, "f_dtRank(string state) =>", lifecycle + "\nf_dtRank(string state) =>")
    before = replace_once(before, '    DtObservation latest = array.get(dtObservation, 0)',
                          '    f_dvShadowEvent(setup, state, reason)\n    DtObservation latest = array.get(dtObservation, 0)')
    display = replace_once(display, "rcViewHasPlan = rcSupported and rcStage == RC_READY and not na(rcShownPlan) and not na(rcKnownAt)",
                           "rcViewHasPlan = rcSupported and dvPlanVisible and not na(rcShownPlan) and not na(rcKnownAt)")
    # The dashboard fragment supplies state aggregation and the two prominent cards.
    return finish(before + marker + snapshot + "\n" + part("permission") + "\n", display, alerts)


def finish(before, display, alerts):
    anchor = "// Presentation only. The RC2 grid retains six logical columns, each spanning two strip segments."
    display = replace_once(display, anchor, part("dashboard") + "\n" + anchor)
    display = replace_once(display, "array.from(0, 1, 2, 3, 6, 7, 10, 15, 16, 17, 18, 19, 20)",
                           "array.from(0, 1, 6, 7, 10, 15, 16, 17, 18, 19, 20)")
    display = replace_once(display, "array.from(4, 5, 11, 12)", "array.from(2, 3, 4, 5, 11, 12)")
    start = "    bool directional = rcStage == RC_READY or rcStage == RC_CONFIRMED"
    end = "\n\n    if rcViewDetailed\n        string trends"
    left, rest = display.split(start, 1)
    _, right = rest.split(end, 1)
    header = (
        '    f_rcViewCell(0, 0, "  QIRVANTA GOLD PRO RC2", rcViewGold, rcViewGraphite, rcViewTitle, text.align_left, true)\n'
        '    f_rcViewCell(0, 1, f_dvContextLine(chartName), rcViewMuted, rcViewGraphite, rcViewSmall, text.align_left)\n'
        '    f_dvDashboard()\n'
        '    f_dvBiasRow()'
    )
    display = left + header + end + right
    display = replace_once(display,
                           '        f_rcViewCell(0, 6, "TREND · M5 / M15 / H1 / H4", rcViewMuted, rcViewRaised, rcViewSmall)\n', "")
    display = replace_once(display, "table.clear(rcViewTable, 0, 6, 11, 9)",
                           "table.clear(rcViewTable, 0, 7, 11, 9)")
    return (before + display + "\n" + alerts.rstrip() + "\n\n" + part("alerts") + "\n" +
            part("paper_qa") + "\n" + part("state_qa"))


def qa_variant(source, start, end, bars, baseline=False):
    source = qa_script(source, start, end, bars, False)
    if baseline:
        source = rename(source, BASE_NAME, BASE_SHORT, "Directional Visibility · baseline probe", "DTV BASE")
        source = source.rstrip() + "\n\n" + part("paper_qa")
    for setting in ("dtShow", "dtQa") + (() if baseline else ("dvNotify",)):
        source = replace_once(source, f"{setting} = input.bool(false,", f"{setting} = input.bool(true,")
    return source


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("indicator", "baseline", "feature"))
    parser.add_argument("probe", type=Path, nargs="?")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--bars", type=int, default=1200)
    parser.add_argument("--start", type=utc_ms, default="2026-11-11T00:00:00Z")
    parser.add_argument("--end", type=utc_ms, default="2026-11-11T17:30:00Z")
    args = parser.parse_args()
    if args.bars < 1 or args.start >= args.end:
        parser.error("Invalid QA range or history length")
    base = pinned(BASE, BASE_SHA)
    if args.kind == "indicator":
        if args.probe:
            parser.error("Indicator has a fixed research path")
        source = build(base).encode("utf-8")
        if args.check:
            if not OUTPUT.is_file() or OUTPUT.read_bytes() != source:
                parser.error("Generated indicator does not match")
        else:
            OUTPUT.parent.mkdir(parents=True, exist_ok=True)
            OUTPUT.write_bytes(source)
        return
    if not args.probe or args.probe.exists() or args.check:
        parser.error("Provide a new output path for the disposable probe")
    source = base if args.kind == "baseline" else build(base)
    data = qa_variant(source, args.start, args.end, args.bars, args.kind == "baseline")
    args.probe.parent.mkdir(parents=True, exist_ok=True)
    args.probe.write_bytes(data.encode("utf-8"))


if __name__ == "__main__":
    main()
