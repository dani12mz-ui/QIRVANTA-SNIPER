# Premium V2 · Native acceptance

**`PARTIALLY_COMPLETE`** · OANDA:XAUUSD standard candles, separate authorized TradingView test profile, 2026-11-10 UTC. Nothing was sent to a broker.

## Verified

The complete checked-in [Premium V2 Pine v6](../unified/experimental/QIRVANTA_SNIPER_PREMIUM_V2.pine) was pasted into the native Pine Editor and read back byte for byte: SHA-256 `a920f15370b536f19def0205b21915e1a3d2e4ed4c76c80bd22f5e8aa6bf7c10`. The editor reported **Compiled** and **Updated on chart**. [Compilation screenshot](premium_v2_evidence/QIRVANTA_PREMIUM_V2_release_compile.jpg).

| Frame | Final V2, S5 and 1,200 calculated bars | Actual capture |
|---|---|---|
| M5 | BUY and SELL submissions; latest BUY closed by SL. The card shows net −1.09R / −13.09 USD for that campaign. | [Desktop](premium_v2_evidence/QIRVANTA_PREMIUM_V2_release_S5_M5.jpg) |
| M15 | Closed SELL; plan 4120.060, fill 4120.008, initial SL 4135.418, scheduled protection 4119.238. TP1 and TP2 executed; TP3 was not filled. The last SL part filled at 4119.240, distinct from the scheduled stop. Campaign net +0.99R / +45.69 USD. | [Desktop](premium_v2_evidence/QIRVANTA_PREMIUM_V2_release_S5_M15.jpg) · [Native trade rows](premium_v2_evidence/QIRVANTA_PREMIUM_V2_release_S5_M15_report.jpg) |
| M30 | Strategy and card render. S5 had no trades in the 1,200-bar window. A separate 2,200-bar UI probe still showed no plan. | [Desktop](premium_v2_evidence/QIRVANTA_PREMIUM_V2_release_S5_M30.jpg) · [Extended probe](premium_v2_evidence/QIRVANTA_PREMIUM_V2_M30_2200_probe.jpg) |
| 390 × 844, M15 | Browser viewport with explicit Mobile preset; the closed SELL card, prices and outcomes are legible. The narrowed viewport shows less price history. This is not a test of the native mobile app. | [Mobile preset](premium_v2_evidence/QIRVANTA_PREMIUM_V2_release_S5_M15_mobile_390.jpg) |

The reference is the user-supplied `QIRVANTA S5 Gold Sell Dashboard.png` (SHA-256 `83c05a21c22f8e316b101f5646a64bfd4ed68985e2bbec5fee2b63b0616ba97f`). Compared against it, V2 uses a dark upper-right card, red SELL and SL, white fill, cyan targets, a protection flag and a close result. The real M15 screenshot uses a different native SELL and shows only two confirmed targets. It also displays more explicit plan/fill, risk, result and context rows. TradingView's rectangular table, native execution glyphs and label styles differ from the reference artwork. Some native glyphs crowd the submission badges, and the 390 px screenshot primarily verifies the dashboard rather than the complete trade geometry.

The separate [Intelligence indicator](../unified/experimental/QIRVANTA_SNIPER_V2_INTELLIGENCE.pine) also compiled and rendered on M15. SHA-256 `4b4bfc9faeb61082596d52052b6a05d884ca66c95b9aaad3c843edeb4346642f`. This verifies chart behavior, not predictive quality. [Compilation](premium_v2_evidence/QIRVANTA_PREMIUM_V2_intelligence_compile.jpg) · [Chart](premium_v2_evidence/QIRVANTA_PREMIUM_V2_intelligence_M15.jpg).

## Native Strategy Report comparison

Both arms ran in the same test layout with 1,200 calculated bars, USD 100,000, fixed quantity 3, USD 0.175 per contract per fill, two ticks of native slippage, standard candles, default detail and execution on close. The temporary V1 control is the pinned V1 source with only the calculation range and short display title changed (SHA-256 `512e1b3868ea083277b6f0bfac3a59ae8051204be2427d3dbe8fd2bd8dda1b24`). The saved V1 and repo file were untouched.

| Mode | Frame | Report dates (2026) | Visible rows matched / total parts | P&L USD | Profit factor | Max drawdown USD |
|---|---|---|---:|---:|---:|---:|
| S0 | M5 | Nov 4–10 | 18/21 | −109.74 | 0.084 | 109.74 |
| S0 | M15 | Oct 22–Nov 10 | 6/6 | −25.75 | 0.566 | 59.37 |
| S0 | M30 | Oct 5–Nov 10 | 0/0 | — | — | — |
| S5 | M5 | Nov 4–10 | 18/21 | +72.81 | 3.226 | 32.93 |
| S5 | M15 | Oct 22–Nov 10 | 9/9 | +59.45 | 4.347 | 38.66 |
| S5 | M30 | Oct 5–Nov 10 | 0/0 | — | — | — |

All 51 captured closed-part rows matched exactly as shown by the UI, including sides, fill/exit timestamps, prices, quantities and rounded P&L. The M5 table rendered 18 of 21 rows per arm at capture time. Strategy Report rounded the metrics and did not expose OHLC fingerprints. The two empty M30 cells are evidence of a matching empty report, not evidence of order parity or improvement. [Extracted native GUI matrix and rows](premium_v2_evidence/native_gui.json) · [V1 compilation](premium_v2_evidence/QIRVANTA_PREMIUM_V2_bounded_V1_compile.jpg) · [V1 S5 M15 trades](premium_v2_evidence/QIRVANTA_PREMIUM_V2_bounded_V1_S5_M15_report.jpg) · [V2 S5 M15 metrics](premium_v2_evidence/QIRVANTA_PREMIUM_V2_release_S5_M15_metrics.jpg).

### Exact native journals · 2026-11-10 14:30 UTC

Two separate diagnostic builds added native journal logging to the pinned V1 and final V2 order engines, with the chart renderers removed. Every pair reconciled and matched under the same completed cutoff, first bar, count, both market-data checksums, currency, point value and minimum tick. The V2 M5 UI limit was raised from 1,200 to 1,201 after the live chart advanced one bar; that restored the original first bar without discarding warmup or journal rows. There are **57 closed parts per arm**, grouped into 19 completed campaigns. Both M30 runs have zero parts.

| Frame | Confirmed bars to cutoff | Two data checksums | S0 closed parts | S5 closed parts |
|---|---:|---|---:|---:|
| M5 | 1,198 | 375274673 / 337323372 | 21 | 21 |
| M15 | 1,199 | 20985408 / 934113557 | 6 | 9 |
| M30 | 1,199 | 640647893 / 889755413 | 0 | 0 |

The [comparison result](premium_v2_evidence/native_pair_20261110_1430Z.json) reports `MATCHED_OBSERVER_JOURNALS` for all six cells. It compares plan submissions, native entry and exit rows, campaign accounting and native equity statistics. [12 original CSVs](premium_v2_evidence/native_pair_20261110_1430Z_logs.zip) · [Pine Logs capture](premium_v2_evidence/QIRVANTA_PREMIUM_V2_pine_logs_M30.jpg). The diagnostics do not replace the separate full-source chart runs. The checked-in [observer helper](../unified/tools/premium_v2_native_pair.py) can regenerate the two buffers at this cutoff and repeat the comparison.

## Runtime and persistence

- The existing V1 instance and uncapped V2 both raised native `RE10110`: *The script takes too long to execute. The time limit is 0.5 seconds.* The uncapped V2 compiled before this runtime error on M5/M15/M30. The lean diagnostic also hit the error on M5; a no-order strategy ran on M15. [Before](premium_v2_evidence/QIRVANTA_PREMIUM_V2_before_runtime.jpg) · [Uncapped V2](premium_v2_evidence/QIRVANTA_PREMIUM_V2_final_compile_M30.jpg) · [Lean diagnostic](premium_v2_evidence/QIRVANTA_PREMIUM_V2_observer_M5_RE10110.jpg) · [Minimal control](premium_v2_evidence/QIRVANTA_PREMIUM_V2_control_compiled_M15.jpg).
- A disposable `calc_bars_count=480` probe loaded, and its 1,200-bar setting produced observed native fills. The final V2 uses the configurable 1,200-bar default; TradingView supports 0 for all available bars. The same date filter alone does not guarantee the same warmup or source OHLC. The reported chart loading incident overlapped the failures, but the exact source of the runtime quota was not established. [TradingView status](https://status.tradingview.com/) · [Calculation range](https://www.tradingview.com/pine-script-docs/language/declaration-statements/#calc_bars_count).
- Private **Save script** returned *Your plan has a limit — 1 saved script*. The test profile's one slot already contains V1. No overwrite, purchase, or public Pine publish was performed. V2 persistence after reload is blocked until the test profile has a free private script slot. [Native save refusal](premium_v2_evidence/QIRVANTA_PREMIUM_V2_private_save_limit.jpg).
- The test chart is left open on the final S5 M15 draft with 1,200 calculated bars and the management master off. Autosave is off in that test session to protect the saved layout. [Final live test draft](premium_v2_evidence/QIRVANTA_PREMIUM_V2_final_test_draft.jpg).

## Open acceptance

- Full-history native parity remains open. The verified V1–V2 parity applies to the aligned bounded window above. The existing [V1 full-history archive](PREMIUM_NATIVE_VERIFICATION.md) is evidence for V1 and cannot be relabeled V2.
- Historical submit badges are attached to the observed confirmation bar; target checks require recorded native exit events. Selecting the historical M15 date in Bar Replay returned an explicit Basic plan refusal for intraday replay. A frozen active plan and replay-through-confirmation verification remain open. [Replay refusal](premium_v2_evidence/QIRVANTA_PREMIUM_V2_intraday_replay_blocked.jpg).
- New standalone signal observations and management variants are experimental. Four M5 native management arms ran in the same bounded data window; the fixed control matched exactly. [Quant study](PREMIUM_V2_QUANT.md) · [Management audit](PREMIUM_V2_MANAGEMENT.md). There is no established performance improvement.
