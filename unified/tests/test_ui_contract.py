"""Static dependency/resource checks; these do not claim Pine rendering correctness."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT.parent / 'refactored'

def source(name):
    return (ROOT / 'src' / (name + '.pineinc')).read_text(encoding='utf-8')

def uncomment(value):
    return '\n'.join(line.split('//', 1)[0] for line in value.splitlines())

class PresentationIsolation(unittest.TestCase):
    def test_overlay_table_is_in_front_of_candles(self):
        # Reproduced in the actual H4 chart: the default behind_chart=true lets
        # candle bodies cover opaque table cells. This is a declaration constant,
        # not an execution input; applying it requires a new chart instance.
        header = source('native_header')
        self.assertRegex(header, r'\bbehind_chart\s*=\s*false\s*[,)]')
        self.assertNotRegex(header, r'\bbehind_chart\s*=\s*input\.')

    def test_lower_table_reserves_transparent_space_for_platform_watermark(self):
        # TradingView draws its watermark over bottom-left anchored content.
        # The footer is a separate empty row, and clear() includes its last index.
        visual = source('visual_alerts')
        self.assertIn('table.new(panelAnchor, 2, panelRows + 1, bgcolor=color.new(ink, 100)', visual)
        self.assertIn('table.clear(dashboard, 0, 0, 1, panelRows)', visual)
        footers = [line for line in visual.splitlines() if 'table.cell(dashboard,' in line
                   and 'panelRows, ""' in line]
        self.assertEqual(len(footers), 2)
        for line in footers:
            self.assertIn('height=str.contains(panelPosition, "Долу") ? 6 : 0', line)
            self.assertIn('bgcolor=color.new(ink, 100)', line)

    def test_pending_quantity_is_requested_not_an_open_position(self):
        # A submitted but unobserved order must not label the requested quantity
        # as a remaining/open position. Read native remaining size only after fill.
        line = next(x for x in source('visual_alerts').splitlines() if 'string entryText =' in x)
        pending, filled = line.split(' : ', 1)
        self.assertIn('nativeCampaignQty', pending)
        self.assertIn('Изпълнено 0', pending)
        self.assertNotIn('shownRemainingQty', pending)
        self.assertIn('shownRemainingQty', filled)
        self.assertIn('Остатък', filled)

    def test_close_only_open_market_load_renders_without_waiting_for_next_close(self):
        # In a close-only strategy, the current open real-time bar may not run.
        # The last confirmed historical bar is not islast in that situation.
        # Both table and reused price objects therefore need this second render route.
        source_text = source('visual_alerts')
        guards = re.findall(r'^if (barstate\.[^\n]+)', source_text, re.M)
        rendering = [guard for guard in guards if 'islast' in guard]
        self.assertEqual(len(rendering), 2)
        for guard in rendering:
            self.assertEqual(guard, 'barstate.islastconfirmedhistory or barstate.islast')
        for is_last, is_last_confirmed, expected in ((False, True, True),
                                                   (True, False, True),
                                                   (False, False, False)):
            self.assertEqual(is_last or is_last_confirmed, expected)

    def test_visual_controls_have_no_dependency_in_trading_modules(self):
        controls = []
        for line in source('inputs').splitlines() + source('visual_alerts').splitlines():
            if 'group="06 / ИЗГЛЕД"' in line and 'input.' in line:
                controls.append(re.match(r'\w+\s+(\w+)\s*=', line).group(1))
        self.assertGreater(len(controls), 10)
        # The full candidate/discovery, shadow gates and native order modules
        # cannot inspect a display control, so toggling the UI cannot branch orders.
        logic = '\n'.join(uncomment(source(name)) for name in
                          ('features', 'types_helpers', 'decision', 'paper_engine', 'native_adapter'))
        for name in controls:
            with self.subTest(control=name):
                self.assertIsNone(re.search(r'\b' + name + r'\b', logic))

    def test_visual_module_cannot_place_orders_or_select_another_plan(self):
        code = uncomment(source('visual_alerts'))
        self.assertIsNone(re.search(r'strategy\.(?:entry|order|exit|close|close_all|cancel|cancel_all)\s*\(', code))
        self.assertIsNone(re.search(r'\bf_(?:pick|makePlan|capture|submit)\s*\(', code))
        self.assertNotRegex(code, r'\b(?:nativePlan|shownPlan|selectedNativePlan)\s*:=')

    def test_discovery_inputs_match_fixed_baseline_exactly(self):
        def declarations(path):
            lines = path.read_text(encoding='utf-8').splitlines()
            return [line for line in lines if 'input.' in line
                    and 'group="06 / ИЗГЛЕД"' not in line
                    and 'group="07 / ИЗВЕСТИЯ"' not in line]
        self.assertEqual(declarations(BASE / 'src/inputs.pineinc'),
                         declarations(ROOT / 'src/inputs.pineinc'))

    def test_histories_fit_declared_object_budget_without_sharing_pools(self):
        inputs = source('inputs')
        visual = source('visual_alerts')
        header = source('native_header')
        def maximum(name):
            line = next(x for x in inputs.splitlines() if x.startswith('int ' + name + ' ='))
            return int(re.search(r'maxval=(\d+)', line).group(1))
        limit = int(re.search(r'max_labels_count=(\d+)', header).group(1))
        # Five reused price labels and one reused preparation label.
        self.assertLessEqual(maximum('keepLabels') + maximum('keepSignalLabels') + 6, limit)
        self.assertIn('label.delete(array.shift(signalLabels))', visual)
        self.assertIn('label.delete(array.shift(contextLabels))', visual)
        self.assertIn('var array<label> signalLabels', visual)
        self.assertIn('var array<label> contextLabels', visual)

    def test_marker_is_admitted_order_not_an_unfilled_candidate(self):
        visual = uncomment(source('visual_alerts'))
        marker = visual.split('Event visualSubmitted = na', 1)[1].split('var array<label> signalLabels', 1)[0]
        self.assertIn('e.book == "NATIVE" and e.kind == "NATIVE_SUBMITTED"', marker)
        self.assertNotIn('"CONFIRMED"', marker)
        self.assertNotIn('"CORE"', marker)
        self.assertNotRegex(marker, r'offset\s*=\s*-')
        self.assertIn('e.planId', visual)

    def test_default_mobile_display_does_not_enable_rejection_history(self):
        inputs = source('inputs')
        self.assertRegex(inputs, r'panelPosition = input.string\("Долу вляво"')
        self.assertRegex(inputs, r'showContextLabels = input.bool\(false')
        self.assertRegex(inputs, r'showSetupLevel = input.bool\(false')
        self.assertIn('uiMode == "DEBUG" and showContextLabels and showHistoricalLabels', source('visual_alerts'))
        self.assertRegex(inputs, r'showHistoricalLabels = input.bool\(false')
        self.assertNotIn('string displayBook =', inputs)

if __name__ == '__main__':
    unittest.main()
