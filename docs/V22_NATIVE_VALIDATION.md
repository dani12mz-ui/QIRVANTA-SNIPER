# QIRVANTA V2.2 native validation

| Status | Scope |
|---|---|
| VERIFIED | Both final Pine v6 files compiled and ran in TradingView on standard OANDA:XAUUSD M5, M15 and M30. Twelve strategy journals reconcile to native cash and next-bar execution. |
| VERIFIED | Native indicator observations reproduced on 10,553 bars with 15 same-start prefix checks. Final dashboard edit matched the preceding fixed ABC order ledger on M5, M15 and M30 exactly. |
| EXPERIMENTAL | Models A, B and C remain independent of S5. The archive has no untouched forward holdout. |
| REJECTED | Promote V2.2 or combine it with S5 based on the available results. On archived M5, the shared slot lost money and increased drawdown under identical data and costs. |
| INCONCLUSIVE | Fresh S5 execution parity across all frames, live intrabar observation and strategy rendering at phone width. |
| BLOCKED | Saving both V2.2 scripts to the test profile. The existing script fills the TradingView Basic limit of one saved script. |

## Native setup

The authorized test profile ran the compiled strategy and Intelligence overlay in separate browser tabs with Autosave off. Both tabs were left open as drafts. A new private script save displayed **Your plan has a limit — 1 saved script**. The minimum action to persist both drafts is to use a test profile with two free private-script slots. [Native save limit](v22_evidence/screens/V22_basic_save_limit.jpg)

| Artifact | Final file SHA-256 | Compilation |
|---|---|---|
| [Strategy](../unified/experimental/QIRVANTA_SNIPER_V22_STRATEGY.pine) | `51e07f901c0a26600ec040772ec4ad915d5d68a61a4e70e946c98bb79c301ffe` | [Pine Editor](v22_evidence/screens/V22_strategy_compile_final.jpg) |
| [Intelligence](../unified/experimental/QIRVANTA_SNIPER_V22_INTELLIGENCE.pine) | `05a563a4a481a8806f604b61292f836c70f058a0b78bcba329f0b55652df91bd` | [Pine Editor](v22_evidence/screens/V22_indicator_compile_final.jpg) |

The strategy runs on all bars delivered to the chart with no script calculation cap. These are finite chart-history runs. The native journals end November 10, 2026 at 15:00 UTC. The underlying strategy declaration uses USD, 100,000 starting equity, three one-unit exit allocations per plan, one position, 100% margin, two adverse ticks on market/stop orders and 0.175 USD commission per unit on every fill. Next-tick execution is enabled through the default order timing, with Bar Magnifier and fill recalculation off. The estimated 0.35 price-unit spread budgets admissions and cost break-even. The offline sensitivity test charges the additional spread and fee stress explicitly. Native same-bar outcomes follow the TradingView emulator's OHLC path. The separate conservative replay evaluates an ambiguous stop before a target. [TradingView strategy execution](https://www.tradingview.com/pine-script-docs/concepts/strategies/)

The indicator limits calculation to 3,600 bars by default. The first compiled 1,200-bar M5 version lacked initialized H4 EMA55 across its test window. At the final setting, 744 audited M5 bars have initialized H4 context. The lower frame observes only completed H1/H4 candles, and the panel explicitly shows WARMUP until the relevant history initializes. The final bounded indicator and uncapped strategy can seed their H4 EMAs differently. The native strategy's lifecycle and execution journal determines actual admission. Neither final file raised RE10110 in the checked runs. Earlier full Premium versions encountered that runtime error. [Confirmed higher timeframes](https://www.tradingview.com/pine-script-docs/concepts/other-timeframes-and-data/) · [Indicator replay](v22_evidence/intelligence_causality_final.json) · [Earlier Premium runtime evidence](V21_NATIVE_VALIDATION.md)

## TradingView emulator results

Fixed 1R/2R/3R, A/B/C enabled, confirmed opposing H1/H4 veto on, A displacement requirement off. A campaign groups all partial closes by original entry ID. PF here uses closed campaign net gains and losses. TradingView's screenshot reports PF at the partial-trade level. Native equity drawdown uses the emulator path. The archive's campaign-close drawdown is reported separately.

| Frame | First loaded bar UTC, 2026 | Bars | Campaigns / closed parts | Net USD | Campaign PF | Native DD USD |
|---|---|---:|---:|---:|---:|---:|
| M5 | Oct 11 22:00 | 5,988 | 35 / 103 | −202.314 | 0.677 | 243.544 |
| M15 | Aug 2 22:00 | 6,586 | 49 / 144 | +435.613 | 1.450 | 277.738 |
| M30 | Jan 1 23:00 | 10,162 | 96 / 284 | −711.939 | 0.838 | 1,498.714 |

Every native M5 row below uses the same price/context fingerprints. All accepted fills occurred on the bar following order submission, with no overlapping positions. The journal records plan and entry IDs, fees, parts, TP counts, reasons for closing and results by model and side. The nine non-fixed-ABC runs use the immediately preceding source template. The final source changed only the dashboard drawing condition. All three fixed ABC reruns on the final template reproduced their prior data fingerprints, campaigns, partial exits, cash and drawdown exactly. [Native matrix](v22_evidence/native_matrix.json) · [Native logs and SHA-256 manifest](v22_evidence/native_logs.zip)

| M5 run | Campaigns | Net USD | Native DD USD |
|---|---:|---:|---:|
| Model A only, fixed | 28 | −129.809 | 197.304 |
| Model B only, fixed | 17 | −234.039 | 234.039 |
| Model C only, fixed | 4 | −14.717 | 42.339 |
| ABC, A fixed targets | 35 | −202.314 | 243.544 |
| ABC, B cost break-even after TP1 | 36 | −171.527 | 202.298 |
| ABC, C 0.05R lock after TP1 | 36 | −164.202 | 193.191 |
| ABC, D ATR trail | 39 | −187.239 | 193.180 |
| ABC, E confirmed swing trail | 39 | −276.283 | 281.968 |
| ABC, F time exit | 35 | −165.414 | 261.232 |
| ABC, G momentum deterioration | 37 | −190.777 | 211.475 |

The V2.2 commit adds files to the pinned parent. The original S0, S5, Champion, Premium sources and evidence are byte-identical. The unchanged S5 observer's fresh replay at the archived 14:30 UTC cutoff matched M15 data fingerprints, fills and exits. Both fingerprints diverged on M5 and M30. M5's trade journal also diverged. Both M30 journals were empty. The fingerprints combine chart and confirmed HTF inputs and cannot identify which input diverged. This makes full fresh native S5 parity inconclusive. The independent evaluation instead verifies the frozen S5 archive on its own original bars before comparing S5 with V2.2. [S5 control](v22_evidence/s5_control_rerun.json) · [Historical comparison and cost stress](V22_HISTORICAL_EVALUATION.md)

## Actual TradingView captures

| Frame | Strategy chart | Strategy Report | Intelligence |
|---|---|---|---|
| M5 | [Case chart and dashboard](v22_evidence/screens/V22_strategy_M5.jpg) | [M5 report](v22_evidence/screens/V22_strategy_M5_report.jpg) | [M5 overlay](v22_evidence/screens/V22_indicator_M5.jpg) |
| M15 | [Mobile preset at desktop width](v22_evidence/screens/V22_strategy_mobile_preset.jpg) | [M15 report](v22_evidence/screens/V22_strategy_M15_report.jpg) | [M15 overlay](v22_evidence/screens/V22_indicator_M15.jpg) |
| M30 | [Compact dashboard](v22_evidence/screens/V22_strategy_M30.jpg) | [M30 report](v22_evidence/screens/V22_strategy_M30_report.jpg) | [M30 overlay](v22_evidence/screens/V22_indicator_M30.jpg) |

The strategy panel distinguishes planned and filled prices, Initial and Current SL, targets, realized result and closed status. A close-only strategy initially waited for the realtime candle to close before drawing the panel. The final code also draws on the last confirmed historical candle. M15 and M30 captures show the panel with the current candle still open. Native order labels overlap part of the panel at the M5 case zoom. The indicator panel was visible at an actual 390 × 844 browser viewport. Native TradingView mobile app rendering and a phone-width strategy panel remain unverified. The original SELL reference has a dark canvas and a custom panel. The captures retain the test layout's light canvas and use native chart markers with the V2.2 dark table. [390 × 844 indicator](v22_evidence/screens/V22_indicator_mobile_390.jpg) · [November 10 M5 chart](v22_evidence/screens/V22_Nov10_case_M5.jpg)

## Reproduce

From the repository root, run the generators in check mode and the local regression suite.

```powershell
python -X utf8 -B -m unified.tools.build_v22_strategy --check
python -X utf8 -B -m unified.tools.build_v22_intelligence --check
$env:PYTHONPATH = "$pwd;$pwd/unified"
python -X utf8 -B -m unittest discover -s unified/tests
```

In TradingView use a fresh strategy draft on OANDA:XAUUSD standard candles. Select **Logging → Native journal** and set **Native journal cutoff close (UTC)** to November 10, 2026 at 15:00. Keep the original strategy Properties. Export Pine Logs as CSV for fixed ABC on each frame, fixed A/B/C separately on M5, and the six other management modes on M5. In **Lifecycle** mode use November 10 at 00:00 UTC as the log start and the same cutoff. For Intelligence enable **Export observations to Pine Logs** and **Include every OHLC / HTF row**. A repeat uses the same first loaded bar, cutoff and fingerprints for exact comparisons. The full source logs stay in the local audit directory. The public archive preserves every native campaign export, lifecycle states and actual fill prices, and indicator observation states and reasons.

```powershell
$evidence = '../../outputs/v22'
$before = Get-ChildItem -LiteralPath $evidence -Filter 'strategy_M*_ABC_A.csv'
$other = Get-ChildItem -LiteralPath $evidence -Filter 'strategy_M*.csv' | Where-Object {$_.Name -notmatch 'ABC_A|_lifecycle'}
$fixed = Get-ChildItem -LiteralPath $evidence -Filter 'strategy_final_*.csv'
$lifecycle = Get-ChildItem -LiteralPath $evidence -Filter 'strategy_*_lifecycle.csv'
python -X utf8 -B -m unified.tools.audit_v22_native --journal $other.FullName $fixed.FullName --lifecycle $lifecycle.FullName --pre-display $before.FullName --out native-check.json
python -X utf8 -B -m unified.tools.audit_v22_case --logs-dir $evidence --output case-check.json --tapes case-check.json.gz
```

[Regime, three models and indicator audit](V22_MARKET_MODELS.md) · [November 10 case](V22_NOVEMBER_10_CASE.md) · [Historical protocol and results](V22_HISTORICAL_EVALUATION.md)
