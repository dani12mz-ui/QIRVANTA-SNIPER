"""Assemble the separate TradingView-only Pine strategy from the pinned champion."""
import argparse
from pathlib import Path

if __package__:
    from .build_ultimate import CHAMPION_SHA, ROOT, champion_bytes, replace_once, sha
    from .tradingview_management import patch_management
else:
    from build_ultimate import CHAMPION_SHA, ROOT, champion_bytes, replace_once, sha
    from tradingview_management import patch_management


COMPONENTS = ROOT / "unified/experimental/tradingview"
OUTPUT = ROOT / "unified/experimental/QIRVANTA_TRADINGVIEW_FINAL.pine"
DISPLAY_SOURCE = ROOT / "unified/src/display_context.pineinc"
ALERTS_SOURCE = ROOT / "unified/src/alerts_helpers.pineinc"


INPUTS = """bool tvAllowVolume = input.bool(false, "S6 · Accept the chart volume basis", group="TV · RESEARCH", display=display.none)
bool tvEnglish = input.string("Български", "Chart language / Език", options=["Български", "English"], group="06 / ИЗГЛЕД", display=display.none) == "English"
bool tvAlertConfirmed = input.bool(false, "Техническо потвърждение / Confirmed candidate", group="07 / ИЗВЕСТИЯ", display=display.none)
bool tvAlertClosed = input.bool(true, "Приключен план / Campaign closed", group="07 / ИЗВЕСТИЯ", display=display.none)
"""


def component(name):
    return (COMPONENTS / (name + ".pineinc")).read_text(encoding="utf-8").strip() + "\n\n"


def _core_metadata(source):
    for kind in ("Plan", "Event"):
        anchor = f"type {kind}\n"
        source = replace_once(source, anchor, anchor + '''    string strategyKey = "S0"
    string targetBasis = "R"
    string quality = "UNVERIFIED"
    string qualityNote = ""
''')
    source = replace_once(source, "    e.book := book", "    e.book := book\n    e.strategyKey := tvStrategyMode")
    source = replace_once(source, "    e.evidence := p.evidence", """    e.strategyKey := p.strategyKey
    e.targetBasis := p.targetBasis
    e.quality := p.quality
    e.qualityNote := p.qualityNote
    e.evidence := p.evidence""")
    old = ' + ",\\"plan_id\\":" + f_q(e.planId)'
    meta = (' + ",\\"strategy_key\\":" + f_q(e.strategyKey) + ",\\"target_basis\\":" + f_q(e.targetBasis)'
            ' + ",\\"quality\\":" + f_q(e.quality) + ",\\"quality_note\\":" + f_q(e.qualityNote)')
    return replace_once(source, old, old + meta)


def _alerts(source):
    anchor = ALERTS_SOURCE.read_text(encoding="utf-8").strip()
    revised = anchor.replace('f_model(p.family)', 'f_tvFamily(p.strategyKey, p.family)').replace(
        'f_model(e.family)', 'f_tvFamily(e.strategyKey, e.family)')
    revised = revised.replace('+ "\\nR проекции: TP1 " + f_price(p.tp1)', '+ "\\n" + f_tvTargets(p.targetBasis) + " · TP1 " + f_price(p.tp1)')
    revised = revised.replace('+ "\\nR проекции: TP1 " + f_price(e.tp1)', '+ "\\n" + f_tvTargets(e.targetBasis) + " · TP1 " + f_price(e.tp1)')
    revised = revised.replace(' or e.kind == "NATIVE_CLOSED"', '')
    revised = replace_once(revised, '    bool diagnostic =', '''    bool confirmed = e.book == "CORE" and e.kind == "CONFIRMED" and tvAlertConfirmed
    bool closed = e.book == "NATIVE" and e.kind == "NATIVE_CLOSED" and tvAlertClosed
    bool diagnostic =''')
    revised = replace_once(revised, '(selectedPlan or watch or risk or diagnostic', '(selectedPlan or confirmed or closed or watch or risk or diagnostic')
    revised = replace_once(revised, '    (kind == "ENTRY" or kind == "NATIVE_ENTRY")', '    kind == "NATIVE_CLOSED" ? "СИМУЛИРАН ПЛАН ПРИКЛЮЧЕН" : kind == "NATIVE_EXIT_RESEARCH" ? "СИМУЛИРАН ЗАЩИТЕН ИЗХОД" : kind == "NATIVE_STOP_SCHEDULED" ? "ЗАЩИТЕН СТОП · СЛЕДВАЩ ТИК" : (kind == "ENTRY" or kind == "NATIVE_ENTRY")')
    return replace_once(source, anchor, revised)


def _presentation(source):
    source = source.replace('f_model(shownPlan.family)', 'f_tvFamily(shownPlan.strategyKey, shownPlan.family)')
    source = replace_once(source, 'f_planTitle(e.side) + "\\n" + f_model(e.family)',
                          'f_planTitle(e.side) + "\\n" + f_tvFamily(e.strategyKey, e.family)')
    source = replace_once(source, 'd == 1 ? "ПЛАН ЗА ПОКУПКА" : "ПЛАН ЗА ПРОДАЖБА"\nf_wrapUi',
                          'd == 1 ? f_tvText("BUY · ПЛАН", "BUY · PLAN") : f_tvText("SELL · ПЛАН", "SELL · PLAN")\nf_wrapUi')
    source = replace_once(source, 'text="ПЛАН", textcolor=upColor', 'text="BUY", textcolor=upColor')
    source = replace_once(source, 'text="ПЛАН", textcolor=downColor', 'text="SELL", textcolor=downColor')
    source = replace_once(source, ' + "\\nSL е структурен. TP1–TP3 са замразени R проекции от ориентир " + f_price(shownReference) + ", не структурни цели."',
                          ' + "\\n" + f_tvTargets(shownPlan.targetBasis) + " · " + f_tvQualityLabel(shownPlan.quality) + "\\n" + shownPlan.qualityNote')
    source = replace_once(source, '        string freshness = shownActive ?', '''        if shownActive
            targetText := "TP1 " + f_price(shownPlan.tp1) + " · " + f_tvR(shownEntry, shownInitialStop, shownPlan.tp1, shownPlan.side) + "\\nTP2 " + f_price(shownPlan.tp2) + " · " + f_tvR(shownEntry, shownInitialStop, shownPlan.tp2, shownPlan.side) + "\\nTP3 " + f_price(shownPlan.tp3) + " · " + f_tvR(shownEntry, shownInitialStop, shownPlan.tp3, shownPlan.side)
            stopText += "\\n" + f_tvProtection(shownPlan, shownEntry, shownStop, shownPending)
        string freshness = shownActive ?''')
    source = replace_once(source, 'string contextText = uiRegime +', 'string contextText = f_tvRegimeLabel(tvRegime) +')
    source = replace_once(source, 'string contextTip = "Информационен контекст; не променя сигналите. H1: "',
                          'string contextTip = "Последни затворени H1/H4. H1: "')
    source = replace_once(source, 'string uiStatus = nativeLastStatus != "" ? nativeLastStatus : uiReason',
                          'string uiStatus = nativeLastStatus != "" ? nativeLastStatus : tvStrategyMode == "S0" ? uiReason : tvStrategyMode + " · " + array.get(tvReasons, uiFocus == 1 ? 0 : 1)')
    source = replace_once(source, 'string clockText = na(lastAnalysisClose) ? "Няма данни" : str.format_time(lastAnalysisClose, "dd.MM HH:mm", riskTimezone)',
                          'string clockText = f_tvText("Последен план ", "Last plan ") + (na(tvLastPlanTime) ? "—" : str.format_time(tvLastPlanTime, "dd.MM HH:mm", riskTimezone))')
    source = replace_once(source, '"Заявка: до следв. затваряне"',
                          'f_tvText("Срок: ", "Expiry: ") + str.tostring(math.max(0, shownPlan.expiresAfterBar - bar_index)) + f_tvText(" св.", " bars")')
    source = replace_once(source, 'f_wrapUi(stageText, 24) + (shownActive ? "\\n" + f_tvFamily(shownPlan.strategyKey, shownPlan.family) : "")',
                          'f_wrapUi(stageText, 24) + (shownActive ? "\\n" + f_tvFamily(shownPlan.strategyKey, shownPlan.family) + " · " + f_tvQualityLabel(shownPlan.quality) : "\\n" + tvStrategyMode)')
    source = source.replace('"Цели R", targetText', '(shownActive ? f_tvTargets(shownPlan.targetBasis) : f_tvText("Цели", "Targets")), targetText')
    source = replace_once(source, 'planTip + "\\nR проекции, не обещани структурни цели."', 'planTip + "\\n" + f_tvText("R:R без разходи", "R:R before costs")')
    source = replace_once(source, '"\\nTP1 R " + f_price(e.tp1) + " | TP2 R " + f_price(e.tp2) + " | TP3 R "',
                          '"\\n" + f_tvTargets(e.targetBasis) + " · TP1 " + f_price(e.tp1) + " | TP2 " + f_price(e.tp2) + " | TP3 "')
    source = replace_once(source, '"TP1 R", "TP2 R", "TP3 R")',
                          'shownPlan.targetBasis == "R" ? "TP1 R" : "TP1", shownPlan.targetBasis == "R" ? "TP2 R" : "TP2", shownPlan.targetBasis == "R" ? "TP3 R" : "TP3")')
    for bg, en in (("Състояние", "Status"), ("Вход / SL", "Entry / SL"), ("Контекст", "Context"),
                   ("Отмяна / риск", "Invalidation"), ("Обновено", "Updated"), ("Решение", "Decision"),
                   ("Емулатор", "Strategy Tester"), ("НЯМА АКТИВЕН ПЛАН", "NO ACTIVE PLAN"),
                   ("Няма потвърден вход", "No confirmed entry"), ("Няма активни нива", "No active levels")):
        source = source.replace(f'"{bg}"', f'f_tvText("{bg}", "{en}")')
    return source


def assemble(champion):
    if sha(champion) != CHAMPION_SHA:
        raise ValueError("Wrong immutable champion bytes")
    source = champion.decode("utf-8")
    source = replace_once(source, 'strategy("QIRVANTA Обединен", shorttitle="QIRV ONE"',
                          'strategy("QIRVANTA TradingView Final · Experimental", shorttitle="QIRV TV"')
    source = replace_once(source, "// Generated by tools/build.py. Exact-hash validation evidence: reports/ACCEPTANCE.json.",
                          "// Generated by tools/build_tradingview_final.py. Experimental Pine v6.")
    source = replace_once(source, "bool alertsEnabled = input.bool(true,", "bool alertsEnabled = input.bool(false,")
    candidate_inputs, candidates = component("candidates").split("// @tv-candidates\n")
    source = replace_once(source, "// BEGIN CANONICAL CORE", INPUTS + candidate_inputs.strip() + "\n\n// BEGIN CANONICAL CORE")
    display = DISPLAY_SOURCE.read_text(encoding="utf-8").strip()
    source = replace_once(source, display, "")
    shared = "\n".join(display.splitlines()[2:])
    shared = shared.replace("if showMTFContext and ", "if ").replace("else if showMTFContext and ", "else if ")
    source = replace_once(source, "// Stateful setup objects", shared + "\n\n" + component("context") + "// Stateful setup objects")
    source = _core_metadata(source)
    source = replace_once(source, "f_makePlan(int family", component("quality") + "f_makePlan(int family")
    source = replace_once(source, "    p\nf_targetRoom", "    f_tvGrade(p)\nf_targetRoom")
    source = replace_once(source, "if processBar\n    array.clear(events)",
                          'if processBar and (tvStrategyMode == "S0" or str.startswith(tvStrategyMode, "F"))\n    array.clear(events)')
    source = replace_once(source, "f_addClosed(Ledger", candidates.strip() + "\n\nf_addClosed(Ledger")
    source = replace_once(source, "// Native fill messages", component("management") + "// Native fill messages")
    source = patch_management(source)
    source = replace_once(source, 'string nativeLastStatus = ""',
                          'var int tvLastPlanTime = na\nstring nativeLastStatus = ""')
    source = replace_once(source, "        nativeAdmittedNow := true",
                          "        f_tvGrade(nativePlan)\n        tvLastPlanTime := time_close\n        nativeAdmittedNow := true")
    source = replace_once(source, "comment=f_model(nativePlan.family)", "comment=f_tvFamily(nativePlan.strategyKey, nativePlan.family)")
    source = _presentation(_alerts(source)).replace("Не е MT5 изпълнение.", "Симулирано ниво.").replace("поръчките НЕ се изпращат в MT5", "симулиран план")
    marker = "// BEGIN CANONICAL CORE "
    start = source.index(marker)
    core_start = source.index("\n", start) + 1
    old_digest = source[start + len(marker):core_start - 1]
    end = source.index("// END CANONICAL CORE", core_start)
    new_digest = sha(source[core_start:end].encode("utf-8"))
    source = replace_once(source, f'CORE_SHA256 = "{old_digest}"', f'CORE_SHA256 = "{new_digest}"')
    return replace_once(source, marker + old_digest, marker + new_digest).encode("utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--build", action="store_true")
    action.add_argument("--check", action="store_true")
    args = parser.parse_args()
    result = assemble(champion_bytes())
    if args.build:
        OUTPUT.write_bytes(result)
    elif OUTPUT.read_bytes() != result:
        raise SystemExit("Generated strategy differs")
    print(f"{OUTPUT.name} sha256={sha(result)}")


if __name__ == "__main__":
    main()
