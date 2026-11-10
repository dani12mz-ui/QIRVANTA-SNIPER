# V2.1 directional event evaluation

Keep S5 as the reference strategy. The experimental impulse model added coverage of some down hours on M5. The combined portfolio took more risk, earned less per campaign and underperformed S5 under cost stress. The sample ends before the November 10 screenshot and was already available to the project.

## Data and event definition

The evaluation used the archived OANDA:XAUUSD OHLC and immutable S5 Pine Logs. Source SHA-256 values, exact bar counts and both native market fingerprints matched. The market outcome definition and impulse defaults were written before running candidate results.

| Frame | Available window UTC | Bars | Complete hours | Eligible hours | BUY events | SELL events |
|---|---|---:|---:|---:|---:|---:|
| M5 | Oct 11 22:00 to Nov 9 02:45, 2026 | 5,565 | 463 | 439 | 21 | 12 |
| M15 | Aug 2 22:00 to Nov 9 02:45, 2026 | 6,445 | 1,610 | 1,547 | 44 | 38 |
| M30 | Jan 1 23:00 to Nov 9 02:30, 2026 | 10,091 | 5,043 | 4,918 | 134 | 130 |

The evaluator checks every complete, non-overlapping UTC hour after at least 250 chart bars and 24 earlier complete hours. An hour qualifies when absolute open-to-close movement is at least 1.5 times the median range of those 24 earlier hours and at least 60% of its own high-low range. Direction comes from the sign of the hourly change. The hour-end classification measures outcomes. Each watch, confirmation and order is computed on available bars before that classification. The horizon and thresholds remained fixed. A large move that straddles the hour boundary may miss this definition. The three frames have different calendars and contain overlapping observations. [Registered definitions](v21_evidence/event_evaluation_registration.json)

## Event coverage and timing

Matching requires a same-direction fill before the event hour ends. Positions carried into the hour, opposite-direction fills, confirmed but rejected risk plans and watches without confirmation are tracked separately. The original S5 archive identifies admitted plans and fills. Its rejected plans and unpublished setup states are unknown on these historical windows.

| Frame | Side | Hours | Impulse watches | Impulse confirms | Impulse risk-valid | S5 fills by hour | Impulse fills by hour | Combined fills by hour |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| M5 | BUY | 21 | 18 | 10 | 0 | 2 | 0 | 1 |
| M5 | SELL | 12 | 11 | 8 | 3 | 0 | 3 | 3 |
| M15 | BUY | 44 | 25 | 10 | 1 | 6 | 1 | 7 |
| M15 | SELL | 38 | 18 | 9 | 1 | 4 | 1 | 4 |
| M30 | BUY | 134 | 34 | 10 | 1 | 10 | 1 | 11 |
| M30 | SELL | 130 | 31 | 19 | 1 | 5 | 1 | 4 |

On the 12 M5 SELL hours, the impulse model first watched at a median 15 minutes among detected hours and first confirmed at a median 27.5 minutes among confirmed hours. The median first fill was 45 minutes from the hour start. For the three fills, the median favorable price excursion was 3.588 USD per quote unit and the median adverse excursion was 1.177. The median favorable capture was 16.18% of the hourly open-to-low movement. The emulator fills at the next available open. For excursion, the evaluation uses full bars before the final exit bar plus observed or modelled exit prices within the hour. Exit-bar extremes and possible book liquidity are unresolved. Five M5 SELL event hours had a confirmed setup rejected by the separate risk gate, three had only a watch and one had neither state. The ten confirmed M5 BUY events all failed the risk gate.

Across all archived hours, M5 produced 195 confirmations, including one pivot retest. It rejected 111 on extension, stop width or both. Of 84 plans whose geometry passed, 18 were blocked by another impulse position. M15 confirmed 223 and M30 confirmed 327. The complete decision tape preserves risk rejection reasons, expired watches, admitted order geometry and matched event excursions. [Evaluation results](v21_evidence/event_evaluation.json) · [Interactive event dashboard](V21_RESEARCH_DASHBOARD.html). The full price-bearing audit tape remains in local `outputs/QIRVANTA_V21_event_evaluation_tapes.json.gz`; its SHA-256 is pinned in the result.

## Portfolio economics

S5 uses recorded native quantities, prices, commissions and partial exits grouped into campaigns. Impulse orders use a separate OHLC replay with three units, 0.175 USD per unit per fill, two ticks adverse on market and stop fills, and frozen stop and limit target. The replay uses stop-first when both levels are touched in the same bar, fills a crossed gap at the next open with applicable slippage, and submits the timeout market order on the close of the 24th holding bar. P&L uses one USD point value, as verified in the source journals. Stress adds 0.30 price units in round-trip spread, 0.10 adverse price units per fill and 50% commission to each original closed campaign. It leaves admissions and fills fixed. The incremental estimates apply to these observed-price and chart-simulation conventions. No historical bid/ask feed or swap data is in this test.

The combined lane uses a single slot and processes both order tapes by decision close. S5 has first priority if two decisions coincide. It blocks overlapping orders and preserves the recorded prices of admitted S5 proposals. Reversing tie priority gave the same archived outcomes. This tests how observed S5 admissions compete with independently reconstructed impulse intents. The S5 proposals that had already been suppressed by original occupancy remain unobserved, so a newly freed slot cannot regenerate them. Combined performance is a constrained counterfactual under that tape assumption.

| Frame | Lane | Closed campaigns | Wins / losses | Net USD | Stressed net USD | Campaign-close DD USD | Mean net R |
|---|---|---:|---:|---:|---:|---:|---:|
| M5 | S5 | 98 | 57 / 41 | +104.531 | −93.919 | 136.309 | +0.0809 |
| M5 | Impulse | 66 | 29 / 37 | −26.181 | −159.831 | 168.291 | +0.0213 |
| M5 | Combined | 139 | 75 / 64 | +139.406 | −142.069 | 229.134 | +0.0804 |
| M15 | S5 | 133 | 65 / 68 | +406.467 | +137.142 | 278.155 | −0.0007 |
| M15 | Impulse | 76 | 23 / 53 | −984.954 | −1,138.854 | 1,045.065 | −0.2181 |
| M15 | Combined | 173 | 71 / 102 | −874.934 | −1,225.259 | 1,180.048 | −0.1083 |
| M30 | S5 | 165 | 89 / 76 | −855.364 | −1,189.489 | 1,968.256 | +0.0243 |
| M30 | Impulse | 99 | 41 / 58 | +380.580 | +180.105 | 678.876 | +0.0743 |
| M30 | Combined | 228 | 116 / 112 | −126.704 | −588.404 | 2,026.172 | +0.0792 |

On M5, the combined lane blocked 18 archived S5 orders and rejected nine impulse intents due to S5 occupancy. Another 16 impulse intents were blocked by impulse occupancy. The ordinary net increase of 34.875 USD fell to a 48.150 USD deficit against S5 under stress. The combined campaign-close drawdown increased 92.825 USD. Its mean net R did not improve. The descriptive 95% campaign-block intervals straddle zero for both S5 and the combined lane.

Outside same-side major hours, the impulse lane recorded 63 M5 trades with 36 losses and −60.393 USD net, 74 M15 trades with 53 losses and −1,051.677 USD net, and 97 M30 trades with 58 losses and +117.030 USD net. These counts show where false-positive and off-event loss risk occurs under the registered label. An off-event win is still counted as a trade and retains its realized P&L. BUY and SELL outcomes and the underlying campaigns remain in the machine report.

The chronological 60/40 split by chart-bar index required campaigns to remain wholly in their evaluation partition. On M5, the combined lane's ordinary net was below S5 earlier and above S5 later, while its stressed net was negative in both partitions. M30's impulse lane earned +886.872 USD earlier and lost −506.292 USD later. M15 impulse lost in both partitions. These are fixed historical subperiods. Promotion still requires an untouched matched evaluation with costs and a portfolio that passes the drawdown gate.

## Reproduction

Run from the repository root. The two input directories contain the original local TradingView exports used by the S5 archive registration. Use new output names for an independent rerun.

```powershell
python -X utf8 -B -m unified.tools.evaluate_v21_impulse --data-root ../mission --logs-dir ../tv-final --output event_evaluation_check.json --tapes event_evaluation_check.json.gz
```

The evaluator verifies input digests and both market fingerprints, reconciles actual S5 fills, checks that its single-slot rule reproduces the observed S5 admissions, and replays the impulse decision prefix at two cuts per frame to verify that later bars cannot change an earlier decision. Five targeted tests passed on bracket ambiguity, gap risk, timeout timing, tie priority and incomplete event hours. A second full run reproduced both output SHA-256 digests. [Evaluator](../unified/tools/evaluate_v21_impulse.py) · [Original archive registration](premium_evidence/s5_screen_registration.json)

The separate native TradingView check matched 418 watch, confirmation and expiry records and 109 order geometry decisions across all three frames. On those completed bars, the offline impulse lane also matched the admitted IDs, timestamps, fill and exit prices and net cash of 27 native closes. The native check covers the bounded runs ending at 15:00 UTC on November 10. The archive dates and combined portfolio remain separate. [Native causality and standalone execution report](v21_evidence/impulse_native_causality.json)
