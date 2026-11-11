# V2.2 on the November 10 XAUUSD decline

The first bearish market-structure shift was visible on M5 at **21:45 UTC on November 9**. The first V2.2 short submission appeared at **13:25 UTC on November 10**. The native emulator filled model A at 4070.128 and stopped all three units at 4080.231 on the following bar, realizing **−31.359 USD**. Model B passed its risk gates on the same confirmation close and lost the single-slot priority to A. M15 and M30 made no short submission in the case. [Native and hypothetical case rows](v22_evidence/screenshot_case.json)

## Exact market interval

The source screenshot clock reads November 10 at 14:52:12 UTC. The analysis begins from the original V2.1 pre-roll at November 9 18:00 UTC and uses only complete bars known by the screenshot clock. M5 and M15 agree on the 4148.710 peak and 4033.915 trough, a decline of **114.795 price units or 2.767%**. M30's last complete bar was known at 14:30 and only captures a low of 4039.720. An intrabar extreme's exact tick timestamp is unresolved. [Original screenshot](premium_v2_evidence/QIRVANTA_PREMIUM_V2_final_test_draft.jpg) · [Earlier S5 investigation](MISSED_SELL_DIAGNOSTIC.md)

| Frame | Peak bar UTC | Trough bar UTC | First bearish structure close UTC | Close through known swing |
|---|---|---|---|---:|
| M5 | Nov 9 20:25–20:30 | Nov 10 14:40–14:45 | Nov 9 21:45, MSS down | 4135.255 through 4136.335 |
| M15 | Nov 9 20:15–20:30 | Nov 10 14:30–14:45 | Nov 10 00:30, MSS down | 4128.695 through 4134.085 |
| M30 | Nov 9 20:00–20:30 | Nov 10 13:30–14:00 | Nov 10 00:30, MSS down | 4128.695 through 4134.085 |

## What the models knew

All times below are November 10 UTC and refer to the decision close. The watch and confirmation times come from native Intelligence records. Rejections, risk acceptance, submission and fills come from the native fixed ABC strategy lifecycle or order journal.

| Frame | Model | First SELL watch | First SELL confirmation | Actual disposition of first confirmation |
|---|---|---:|---:|---|
| M5 | A, structure | 01:00 | 03:45 | HTF conflict. Reconstructed stop was also 4.434 prior ATR against a 3.0 cap. |
| M5 | B, pullback | 01:05 | 07:50 | HTF conflict. |
| M5 | C, consolidation | 02:55 | 03:45 | HTF conflict. Stop distance was 1.986 prior ATR. |
| M15 | A, structure | 13:30 | 13:45 | Volatility spike. True range was 6.096 prior ATR against a 2.50 cap. |
| M15 | B, pullback | 05:30 | 09:30 | HTF conflict. Extension was 3.024 prior ATR against a 1.75 cap. |
| M15 | C, consolidation | 05:15 | — | No close met the continuation confirmation rules by the screenshot. |
| M30 | A, structure | 13:30 | 14:00 | Volatility spike. True range was 4.234 prior ATR against a 2.50 cap. |
| M30 | B, pullback | — | — | No SELL watch in the case. |
| M30 | C, consolidation | — | — | No SELL watch in the case. |

M5 produced seven A confirmations, two B confirmations and one C confirmation. Three A confirmations returned `HTF_CONFLICT`, two returned `EXCESSIVE_EXTENSION` and one returned `INSUFFICIENT_ROOM`. B returned one HTF conflict and one `PORTFOLIO_PRIORITY`. C's sole confirmation returned HTF conflict. The M5 A plan at 13:25 proposed 4070.065, initial stop 4080.229 and targets 4059.901, 4049.737 and 4039.573. Its next-open fill shifted actual initial risk to 30.303 USD for three units. At 13:30 the stop filled all three parts at 4080.231, charging a total 1.050 USD commission. No target filled.

The indicator and strategy load different amounts of history. On M5, the indicator's 3,600-bar setting still showed bullish H4 context at 13:25, while the uncapped native strategy marked both A and B `ENTRY_VALID` on that close. The native fixed run contained 5,988 M5 bars through 15:00. The lifecycle determines actual admission and rejection. The indicator export provides the separate observation and derived geometry; the strategy journal does not expose per-bar EMA values needed to quantify this context difference. The M5 03:45 C plan and M15/M30 late candidates were already past their chart-bar warmup when rejected.

## Diagnostic next-open paths

The table applies the recorded indicator geometry with three equal units and frozen 1R, 2R and 3R exits. It uses the next available open, two ticks of adverse market or stop slippage, 0.175 USD commission per unit per fill and stop-first ordering for unresolved simultaneous levels. Gap guards remain active. These are hypothetical paths for individual candidates in isolation. An unclosed path is censored at the screenshot clock. Excursions stop at final exit and omit unknown exit-bar extremes. The tape retains the entry, stop, targets, all evaluated geometry gates, gross path, costs and partial exit reasons.

| Frame | Model / confirmation UTC | Native reason | Modeled entry | Favorable / adverse price excursion | Diagnostic net USD |
|---|---|---|---:|---:|---:|
| M5 | A 03:45 | HTF conflict | 4109.518 | 1.303 / 2.202 | +2.298, gap-risk exit |
| M5 | C 03:45 | HTF conflict | 4109.518 | 1.303 / 8.422 | −26.316 |
| M5 | A 07:35 | HTF conflict | 4114.863 | 1.743 / 2.927 | −8.082, gap-risk exit |
| M5 | B 07:50 | HTF conflict | 4114.268 | 1.968 / 6.255 | −19.815 |
| M5 | A 09:35 | HTF conflict | 4087.868 | 5.793 / 0.557 | +11.553, gap-risk exit |
| M5 | A 10:20 | Excessive extension | 4080.643 | 26.201 / 0.267 | +51.300 |
| M5 | A 11:10 | Excessive extension | 4070.553 | 1.023 / 8.599 | −26.847 |
| M5 | A 13:25 | Filled | 4070.128 | 2.033 / 10.103 | −31.359 |
| M5 | B 13:25 | Portfolio priority | 4070.128 | 2.033 / 10.103 | −31.359 |
| M5 | A 14:35 | Insufficient room | 4047.298 | 13.383 / 0.317 | Censored |
| M15 | B 09:30 | HTF conflict | 4090.413 | 8.338 / 0.562 | +17.748, gap-risk exit |
| M15 | A 13:45 | Volatility spike | 4056.733 | 7.323 / 6.832 | +12.858, gap-risk exit |
| M30 | A 14:00 | Volatility spike | 4052.093 | 3.353 / 9.362 | Censored |

At 10:20 the M5 short was 2.379 prior ATR from EMA21 and failed the 1.75 gate. Its later path would have filled three targets in isolation for +51.300 USD, or +49.275 USD under registered incremental cost stress. The 11:10 rejection of the same type would have stopped at a loss. This case was held apart from the archive evaluation and the two candidate paths offer no independent justification for changing the cap.

Run from the repository root with the final indicator and strategy Pine CSV exports, plus the strategy lifecycle exports. The command verifies the indicator OHLC fingerprints and the native fixed order ledger.

```powershell
python -X utf8 -B -m unified.tools.audit_v22_case --logs-dir ../../outputs/v22 --output case-check.json --tapes case-check.json.gz
```

[Case evaluator](../unified/tools/audit_v22_case.py) · [Historical matched evaluation](V22_HISTORICAL_EVALUATION.md)

The full case timeline and strategy parts tape stays in the local audit directory as `screenshot_case_tape.json.gz`. Its SHA-256 is `33a11a68f3f9ac0801d40c6dd095df0afe9f88c81cf50b5e4400af5fed738988`.
