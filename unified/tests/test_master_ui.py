"""Presentation regressions. Static/functional checks are not a Pine renderer."""
import re
import unittest
from pathlib import Path
from tools.check_engineering_parity import check

ROOT = Path(__file__).resolve().parents[1]


def source(name):
    return (ROOT / 'src' / f'{name}.pineinc').read_text(encoding='utf-8')


def code(value):
    return re.sub(r'"(?:\\.|[^"\\])*"|//[^\n]*', '', value)


class MasterUI(unittest.TestCase):
    def test_exact_execution_projection_matches_frozen_unified_base(self):
        report = check()
        self.assertEqual(report['differences'], [])
        self.assertEqual(report['baseline_projection_sha256'], report['current_projection_sha256'])
        self.assertTrue(all(item['byte_identical'] for item in report['components'].values()))

    def test_four_density_modes_have_compact_default_and_fixed_rows(self):
        inputs, visual = source('inputs'), source('visual_alerts')
        self.assertIn('uiMode = input.string("STANDARD"', inputs)
        self.assertIn('options=["MINIMAL", "STANDARD", "PRO", "DEBUG"]', inputs)
        self.assertIn('uiMode == "DEBUG" ? 18 : uiMode == "PRO" ? 10 : uiMode == "MINIMAL" ? 4 : 6', visual)
        self.assertIn('if uiMode == "PRO" or uiMode == "DEBUG"', visual)
        self.assertIn('if uiMode == "DEBUG"', visual)

    def test_each_level_and_side_has_a_presentation_switch(self):
        visual = source('visual_alerts')
        # Preserve accessible full labels in separate rows: TradingView CW10026
        # was reproduced with multiple full titles sharing these inline groups.
        self.assertNotRegex(source('inputs'), r'inline="(?:levels1|levels2|side)"')
        self.assertIn('array.from(showEntryLevel, showStopLevel, showTP1Level, showTP2Level, showTP3Level)', visual)
        self.assertIn('if not array.get(visible, i)\n                continue', visual)
        self.assertIn('showSignalMarkers and showBuySignals and technicalBuy', visual)
        self.assertIn('showSignalMarkers and showSellSignals and technicalSell', visual)
        self.assertIn('showExplanatoryText ? f_wrapUi(f_humanReason', visual)

    def test_long_text_is_confined_essential_values_are_not_truncated(self):
        visual, helpers = source('visual_alerts'), source('alerts_helpers')
        self.assertIn('str.substring(word, part * columns', visual)  # hard-wrap unbroken commentary
        self.assertIn('f_humanReason(uiStatus, uiMode == "DEBUG" ? 120 : 64)', visual)
        self.assertIn('f_humanReason(chosenEvent.reason, 30)', visual)
        self.assertIn('f_humanReason(e.reason, 96)', helpers)
        for text in (visual, helpers):
            for match in re.finditer(r'\bf_(?:short|humanReason|wrapUi)\(([^\n]*?)\)', text):
                self.assertNotRegex(match[1], r'\b(?:f_price|planId|p\.id|shownPlan\.id)\b')
        # Full identity belongs to tooltip/messages, not a wide table value.
        self.assertNotRegex(visual, r'f_cell\([^\n]*,\s*shownPlan\.id\s*,')

    def test_clear_and_reuse_includes_hidden_text_and_two_zone_objects(self):
        visual = source('visual_alerts')
        self.assertEqual(visual.count('box.new('), 1)
        init = visual.split('if barstate.isfirst\n', 1)[1].split('if barstate.islastconfirmedhistory', 1)[0]
        self.assertIn('for i = 0 to 1', init)
        self.assertIn('box.new(', init)
        render = visual.split('// Hiding is a setter operation', 1)[1]
        self.assertNotIn('box.new(', render)
        for operation in ('box.set_top(zone, na)', 'box.set_bottom(zone, na)',
                          'label.set_text(array.get(planLabels, i), "")',
                          'label.set_tooltip(array.get(planLabels, i), "")'):
            self.assertIn(operation, render)
        self.assertIn('if not showConfirmedLabels\n    while array.size(signalLabels) > 0', visual)
        self.assertIn('if not showHistoricalLabels or not showContextLabels or uiMode != "DEBUG"', visual)

    def test_display_context_only_requests_strictly_higher_completed_frames(self):
        context = source('display_context')
        for seconds, frame in ((3600, '60'), (14400, '240')):
            block = context.split(f'if showMTFContext and tfSeconds < {seconds}\n', 1)[1].split('else if', 1)[0]
            self.assertIn(f'request.security(syminfo.tickerid, "{frame}"', block)
            for field in ('close[1]', 'ta.ema(close, 50)[1]', 'ta.ema(close, 50)[3]', 'time_close[1]', 'lookahead=barmerge.lookahead_on'):
                self.assertIn(field, block)
            self.assertIn(f'else if showMTFContext and tfSeconds == {seconds}', context)
        self.assertNotIn('request.security_lower_tf', context)
        self.assertIn('tfSeconds > 3600 ? "недостъпен"', source('visual_alerts'))
        self.assertIn('barstate.isconfirmed ? time_close : time_close[1]', context)

    def test_display_context_cannot_feed_orders_or_write_trading_state(self):
        context = code(source('display_context'))
        names = re.findall(r'^(?:float|int|string)\s+(ui\w+)\s*=', context, re.M)
        self.assertGreater(len(names), 8)
        for module in ('features', 'types_helpers', 'decision', 'paper_engine', 'native_adapter'):
            for name in names:
                self.assertNotRegex(code(source(module)), rf'\b{name}\b')
        self.assertNotRegex(context, r'strategy\.|\bf_(?:pick|makePlan|capture|submit)\(')
        for target in re.findall(r'\b(\w+)\s*:=', context):
            self.assertTrue(target.startswith('ui'), target)

    def test_human_alerts_keep_identity_times_prices_without_raw_event_enums(self):
        helpers = source('alerts_helpers')
        message = helpers.split('f_readableEvent(Event e) =>', 1)[1]
        self.assertNotRegex(message, r'\+\s*e\.(?:kind|book)\s*\+')
        for field in ('e.planId', 'f_clockUtc(e.effectiveTime)', 'f_clockUtc(e.knownTime)',
                      'e.knownTime - e.effectiveTime', 'f_price(e.reference)',
                      'f_price(e.stop)', 'f_price(e.tp1)', 'f_price(e.tp2)', 'f_price(e.tp3)'):
            self.assertIn(field, message)
        native = helpers.split('f_nativeMessage(', 1)[1].split('f_isRiskEvent(', 1)[0]
        self.assertIn('bar_index - p.signalBar', native)
        self.assertIn('f_clockUtc(p.signalTime)', native)
        self.assertIn('p.id', native)
        self.assertIn('Точната причина не е записана', native)
        self.assertIn('eventKind == "NATIVE_EXIT_GAP"', native)
        dispatcher = source('alerts')
        self.assertIn('jsonBatch += (jsonBatch == "" ? "" : ",") + f_json(e)', dispatcher)
        self.assertRegex(dispatcher, r'alert\(alertJson\s*\?.*:\s*readableBatch,')


if __name__ == '__main__':
    unittest.main()
