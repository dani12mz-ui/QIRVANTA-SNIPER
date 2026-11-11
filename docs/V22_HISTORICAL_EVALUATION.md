# QIRVANTA V2.2 historical evaluation

**EXPERIMENTAL.** V2.2 improved capture of some downside events on M5, but the shared S5 and V2.2 account fell from +104.531 USD to −17.918 USD before incremental stress. Its campaign-close drawdown rose from 136.309 USD to 224.269 USD. The tested candidates do not meet the promotion gates. [Registered methodology](v22_evidence/historical_registration.json)

## Market data and comparisons

Each frame compares A (structural breakout), B (pullback), C (consolidation) and their shared-slot combination on the same OANDA:XAUUSD standard chart bars as S5. SHA-256, exact time bounds and both original native fingerprints matched. S5 contributed original admitted proposals, actual entry fills, partial exits and recorded commission. A/B/C used the frozen V2.2 rules and an explicit chart-bar execution model. The earlier dates were already observed in V2.1 research.

| Frame | Archive interval UTC | Bars | Qualifying BUY / SELL hours |
|---|---|---:|---:|
| M5 | Oct 11 22:00 to Nov 9 02:45, 2026 | 5,565 | 21 / 12 |
| M15 | Aug 2 22:00 to Nov 9 02:45, 2026 | 6,445 | 44 / 38 |
| M30 | Jan 1 23:00 to Nov 9 02:30, 2026 | 10,091 | 134 / 130 |

The replay reconstructs H1 and H4 from completed chart groups. H4 uses the observed New York session anchors and changes UTC alignment with daylight saving. Confirmed close prices match every exported M5 H1 value and M15 H4 value. Earlier M30 bars supply H1/H4 EMA history for M5 and M15. M30 supplies D rather than H1/H4 in the original journal and has 170 eligible chart bars with fewer than 55 observed H4 or H1 closes. Native H1/H4 EMA seed history can differ from the archived reconstruction. The separate native indicator replay checks the exported context, lower-frame decisions and prefix stability.

## Standalone models and shared capital

Each lane starts with 100,000 USD, three units per plan and one pending or active position. The ABC lane considers models in A/B/C order and vetoes same-close valid candidates on opposing sides. The S5+ABC lane reserves the same slot and cash across both engines and gives S5 first priority at equal decision times. Reversing the engine priority leaves M5/M15 admissions unchanged and changes M30 admissions. A subsequent opening gap can cause a capital failure after submission. The offline engine processes that failure on the next bar and preserves the submitting close's reservation.

The combined comparison holds the native S5 proposal tape and known fills fixed, then admits or blocks those offers in chronological order. It cannot regenerate S5 candidates suppressed before the original journal recorded admission. On M5 the shared lane blocked 12 recorded S5 proposals whose original combined net was +72.227 USD. The overlap ledger preserves the blocked candidate and occupant IDs. [Archived results and complete campaign risk metrics](v22_evidence/historical_results.json)

| Frame | Lane | Campaigns | Wins / losses | Net USD | Stressed USD | PF | Mean net R | Campaign-close DD USD |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| M5 | S5 | 98 | 57 / 41 | +104.531 | −93.919 | 1.150 | +0.0809 | 136.309 |
| M5 | A | 24 | 10 / 14 | −15.713 | −64.313 | 0.960 | +0.0191 | 181.518 |
| M5 | B | 13 | 1 / 12 | −173.080 | −199.405 | 0.289 | −0.8431 | 173.080 |
| M5 | C | 4 | 1 / 3 | −14.717 | −22.817 | 0.615 | −0.1558 | 38.213 |
| M5 | ABC | 30 | 10 / 20 | −94.774 | −155.524 | 0.815 | −0.1885 | 219.081 |
| M5 | S5+ABC | 114 | 59 / 55 | −17.918 | −248.768 | 0.984 | +0.0144 | 224.269 |
| M15 | S5 | 133 | 65 / 68 | +406.467 | +137.142 | 1.195 | −0.0007 | 278.155 |
| M15 | A | 30 | 11 / 19 | +209.212 | +148.462 | 1.293 | +0.0448 | 251.061 |
| M15 | B | 25 | 8 / 17 | +37.752 | −12.873 | 1.086 | +0.0082 | 151.894 |
| M15 | C | 5 | 2 / 3 | +50.992 | +40.867 | 1.579 | +0.2993 | 45.290 |
| M15 | ABC | 49 | 19 / 30 | +435.613 | +336.388 | 1.450 | +0.1445 | 224.122 |
| M15 | S5+ABC | 158 | 71 / 87 | +483.026 | +163.076 | 1.178 | +0.0086 | 553.757 |
| M30 | S5 | 165 | 89 / 76 | −855.364 | −1,189.489 | 0.844 | +0.0243 | 1,968.256 |
| M30 | A | 60 | 22 / 38 | −135.731 | −257.231 | 0.956 | −0.0084 | 1,271.628 |
| M30 | B | 52 | 12 / 40 | −1,036.702 | −1,142.002 | 0.497 | −0.3803 | 1,036.702 |
| M30 | C | 5 | 3 / 2 | +98.323 | +88.198 | 2.167 | +0.2437 | 84.285 |
| M30 | ABC | 96 | 31 / 65 | −775.858 | −970.258 | 0.825 | −0.1313 | 1,477.033 |
| M30 | S5+ABC | 221 | 99 / 122 | −1,000.451 | −1,447.976 | 0.881 | −0.0387 | 2,677.958 |

The fixed simulator fills on the next available chart open plus two adverse market ticks, applies two adverse ticks to stop fills and charges 0.175 USD per unit per fill. Three independent one-unit parts share the structural stop and use frozen 1R/2R/3R targets. When OHLC touches both protection and targets after an unambiguous opening event, the stop is evaluated first. Protection scheduled at the close first applies to the next open. The spread budget of 0.35 price units participates in plan admission and cost break-even. Stress adds 0.30 round-trip spread, 0.10 additional adverse price units per fill, and 50% commission to frozen outcomes. Real historical bid/ask, swap, queue position and tick ordering are unavailable.

M5 ABC incurred 31.500 USD native-equivalent commission and 0.306 USD nominal market/stop slippage already reflected in its simulated prices. Its maximum initial risk was 49.020 USD and maximum entry notional 12,515.439 USD. It had eight consecutive losses at worst and positions touched 757 of 5,565 chart bars. S5 touched 1,030, and S5+ABC touched 1,593. These count entry and exit bars as exposed because exact intrabar hold times are unknown. The JSON also gives win rate, average holding bars and elapsed minutes, charged fees, entry risk, stop movement, gaps and stress drawdown for every lane. Campaign-close drawdown measures the closed cash path.

## Coverage and rejected decisions

The inherited symmetric event rule classifies complete, disjoint UTC hours after enough chart history. An hour needs open-to-close displacement of at least 1.5 times the median range of the preceding 24 complete hours and at least 60% of its own range. Event membership is determined from the outcome and only measures coverage. The evaluator tallies watches, confirmations, geometry-ready intents, admitted orders and fills separately. The S5 archive exposes admissions and fills, with no reliable watch or pre-admission rejection count.

| Frame | Lane | BUY confirms / fills | SELL confirms / fills |
|---|---|---:|---:|
| M5 | S5 observed admissions | 2 / 2 | 0 / 0 |
| M5 | A | 9 / 0 | 6 / 2 |
| M5 | B | 4 / 1 | 2 / 1 |
| M5 | C | 0 / 0 | 0 / 0 |
| M5 | ABC | 10 / 1 | 6 / 2 |
| M5 | S5+ABC | 12 / 3 | 6 / 2 |
| M15 | S5 observed admissions | 6 / 6 | 4 / 4 |
| M15 | ABC | 7 / 2 | 8 / 0 |
| M15 | S5+ABC | 13 / 7 | 12 / 4 |
| M30 | S5 observed admissions | 10 / 10 | 5 / 5 |
| M30 | ABC | 14 / 1 | 19 / 2 |
| M30 | S5+ABC | 24 / 11 | 24 / 5 |

On M5, ABC's first SELL watch arrived at a median 25 minutes and first confirmation at a median 30 minutes in their respective detected hours. The two filled SELL hours entered at a median 12.5 minutes from event start. The single BUY entry arrived at 30 minutes. The matching fills occurred one chart bar after submission. For the two SELL entries, the median favorable excursion was 21.346 price units and adverse excursion was 4.709. The median capture was 65.78% of the hour's open-to-low movement. The measurement stops when the campaign exits or the event ends. An unresolved exit-bar extreme is omitted.

ABC's main M5 rejection reasons were excessive extension (79), opposing H1/H4 context (75) and insufficient room (56). Outside same-side events, ABC closed 27 M5 campaigns with 19 losses and −176.709 USD. S5+ABC closed 109 off-event campaigns with 54 losses and −185.291 USD. Confirmations and favorable moves outside these hour boundaries remain visible in the full tape. The false-positive proxy includes off-event wins and can miss moves that cross an hour boundary.

## Management policies

Each management row uses the same ABC signal tape and recalculates shared-slot occupancy as exits change. A positive net delta fails the risk gate if campaign-close drawdown increases. All runs use conservative intrabar ordering and require forward evidence before promotion.

| Policy | M5 net / stress USD | M5 DD USD | M15 net / DD USD | M30 net / DD USD |
|---|---:|---:|---:|---:|
| Fixed 1R/2R/3R | −94.774 / −155.524 | 219.081 | +435.613 / 224.122 | −775.858 / 1,477.033 |
| Cost BE after TP1 | −68.398 / −131.173 | 183.354 | +387.163 / 212.473 | −687.808 / 1,679.104 |
| 0.05R lock after TP1 | −60.556 / −123.331 | 173.142 | +390.913 / 212.039 | −667.761 / 1,664.121 |
| 2 ATR trail | −100.180 / −167.005 | 120.584 | +168.726 / 281.412 | −1,056.420 / 1,114.743 |
| Confirmed swing trail | −190.425 / −259.275 | 200.463 | +37.264 / 287.685 | −1,061.713 / 1,153.489 |
| 24-bar timer before TP1 | −57.874 / −118.624 | 170.589 | +362.451 / 224.122 | −1,148.425 / 1,408.813 |
| Momentum deterioration | −119.734 / −184.534 | 144.620 | +8.864 / 331.501 | −1,295.022 / 1,364.043 |

The M5 time, break-even and profit-lock variants were still negative under stress. Cost BE and profit lock improved M30 nominal net but violated the drawdown gate. A cost-adjusted stop can still realize a loss after a gap, slippage or a larger spread.

## Chronology, uncertainty and reproduction

The evaluator preserves the fixed 60/40 archival split and reports four M5, 14 M15 and 44 M30 complete Monday-to-Monday evaluation weeks. Each week uses the frozen rules with earlier data for state warmup. The split omits campaigns spanning its boundary from split statistics and preserves them in the full outcome tape. M15 ABC earned 554.105 USD in the earlier portion and lost 118.492 USD in the later portion. In the primary M5 paired stressed-return comparison, all five Holm-adjusted p-values were 1. The test has only six adjacent-day blocks and is descriptive. Every archived period had been examined before this work. An untouched forward holdout remains unavailable.

Run from the repository root with the original local archives. Use new output names for each run.

```powershell
python -X utf8 -B -m unified.tools.evaluate_v22_history --data-root ../mission --logs-dir ../tv-final --output history-check.json --tapes history-check.json.gz
```

The evaluator also checks that S5 reproduces its original one-slot admissions and verifies earlier decisions on two truncated prefixes per frame. During native parity review, rounding the directional-movement comparison to Pine's precision fixed a binary subtraction tie. The correction added one losing M30 A campaign and left M5 and M15 results unchanged. Four targeted execution tests covered ambiguous stop ordering, delayed cost break-even, gap invalidation after a partial target and a next-open margin failure. [Runner](../unified/tools/evaluate_v22_history.py) · [Signal reconstruction](../unified/tools/evaluate_v22_signals.py) · [Execution and management](../unified/tools/evaluate_v22_execution.py)

The full observations, orders and blocked proposals tape stays in the local audit directory as `historical_audit_tape.json.gz`. Its SHA-256 is `b06ca3c57679370c1db68f150d43447b68a84241c905d5a960743ba38e106694`.
