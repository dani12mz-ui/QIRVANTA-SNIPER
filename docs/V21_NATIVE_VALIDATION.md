# QIRVANTA V2.1 · Native validation

**EXPERIMENTAL. S5 remains the reference.** Tested on 10 November 2026 in the separate TradingView test profile, standard `OANDA:XAUUSD` candles, UTC, using temporary Pine drafts only. The [complete strategy](../unified/experimental/QIRVANTA_SNIPER_V21_IMPULSE.pine) was copied back from Pine Editor and matched SHA-256 `31413eb4a936137d9a443143d1b46578a6adc71e69b173dd067d7d8e659831e4`. TradingView reported **Compiled** and **Updated on chart**. [Compilation](v21_evidence/QIRVANTA_V21_native_compile.jpg).

| Frame | 1,200 setting: confirmed bars through 15:00 UTC | Closed | Net USD | Profit factor | Native DD USD | Evidence |
|---|---:|---:|---:|---:|---:|---|
| M5 | 1,198 | 8 | −84.429 | 0.252 | 95.346 | [Chart](v21_evidence/QIRVANTA_V21_native_M5.jpg) · [Strategy Report](v21_evidence/QIRVANTA_V21_native_M5_report.jpg) |
| M15 | 1,199 | 10 | −172.251 | 0.410 | 206.478 | [Chart](v21_evidence/QIRVANTA_V21_native_M15.jpg) · [Strategy Report](v21_evidence/QIRVANTA_V21_native_M15_report.jpg) |
| M30 | 1,199 | 9 | −248.910 | 0.316 | 252.105 | [Chart](v21_evidence/QIRVANTA_V21_native_M30.jpg) · [Strategy Report](v21_evidence/QIRVANTA_V21_native_M30_report.jpg) |

Properties were USD 100,000, fixed 3 units, no pyramiding, two slippage ticks, 0.175 USD per unit per fill, margin 100% both sides, confirmed-close calculation, next-tick execution, Bar Magnifier off, no separate spread in the native properties. [Machine reconciliation](v21_evidence/native_impulse_20261110.json) · [Native decisions and original fill messages](v21_evidence/v21_decisions_and_native_fills.zip). The original TradingView CSVs including the OHLC streams are retained in local `outputs/QIRVANTA_V21_M{5,15,30}_native.csv`. The published ZIP preserves decision and trade rows and records hashes of the full exports.

The bar tapes and Pine logs independently reproduced **418 lifecycle records, 109 risk decisions and 27 closed trades** with matching next-tick times, fills, exits, fees and net cash. Fifteen same-start prefix reruns retained the same already confirmed decisions. This verifies causal closed-bar behavior on these tapes. Live intrabar events and an unlimited range with different warmup do not have the same evidentiary scope. [Verifier and results](V21_IMPULSE_ENGINE.md).

## The screenshot event

The [forensic report](MISSED_SELL_DIAGNOSTIC.md) locates the exact decline and why S5 M15 did not enter. On M5, V2.1's 02:40 SELL filled at 4114.978 and time-exited at 04:40 for **−4.212 USD** net. On the later acceleration, the M5 watch at 09:05 confirmed at 09:10 but rejected the order: **3.591 prior ATR** of extension and **4.034 prior ATR** of initial stop distance, both above **2.50**. M15 watched at 09:15, confirmed at 09:30, and rejected at **3.024 ATR** extension and **3.726 ATR** risk; the later confirmations also failed their risk gates. The example generated actionable observations but no new executable SELL in that acceleration window. No parameters were changed to fit the screenshot.

## RE10110 and parallel display

The original full-history V1 runtime refusal was visible at the beginning of the session. For S5, the observational 1,200- and 2,200-bar controls agreed through the case; those lean probes are not uncapped tests of the full V2 renderer. The smaller independent V2.1 also executed with **Calculated bars = 0**, audit disabled, and showed native results on all three frames without RE10110 in this test:

| Frame | Strategy Report range | Closed | Net USD | PF | Native DD USD |
|---|---|---:|---:|---:|---:|
| [M5](v21_evidence/QIRVANTA_V21_uncapped_M5_report.jpg) | Oct 12 – Nov 10 | 69 | −56.64 | 0.929 | 189.43 |
| [M15](v21_evidence/QIRVANTA_V21_uncapped_M15_report.jpg) | Aug 3 – Nov 10 | 77 | −912.56 | 0.568 | 972.82 |
| [M30](v21_evidence/QIRVANTA_V21_uncapped_M30_report.jpg) | Jan 2 – Nov 10 | 99 | +380.58 | 1.086 | 678.88 |

These uncapped GUI checks use different calculation windows from the matched decision exports. They do not establish unrestricted execution for the old engine or a performance improvement. The shipped default remains 1,200 calculated bars.

The exact unchanged Premium V2 SHA `a920f15370b536f19def0205b21915e1a3d2e4ed4c76c80bd22f5e8aa6bf7c10` was then compiled as a second draft, set to S5 and displayed beside V2.1. [Actual parallel M15 chart](v21_evidence/QIRVANTA_V21_parallel_S5_M15.jpg). Their Strategy Reports remain separate. The [event research dashboard](V21_RESEARCH_DASHBOARD.html) shows S5, impulse and the separately labeled offline one-slot portfolio. Autosave is off in the test layout. No private script was replaced, no layout was saved and no broker was connected.

## Reproduce

```powershell
python -X utf8 -B unified/tools/audit_v21_native.py --logs <three-full-native-csv-files> --out native-check.json
python -X utf8 -B unified/tools/build_v21_dashboard.py --report docs/v21_evidence/event_evaluation.json --tape <local-evaluation-tape> --out dashboard-check.html
```

See [the independent event evaluation](V21_EVENT_EVALUATION.md) for causal event labels, overlapping positions, adverse cost stress, false positives, and the reason S5 was retained.
