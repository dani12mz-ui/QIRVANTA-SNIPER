# Missed SELL diagnostic

## Why S5 M15 displayed no new SELL

On November 10, 2026 through the screenshot's last confirmed M15 close at 14:45 UTC, S5 returned `Няма суров trigger` (no raw trigger) on **all 59 M15 closes**. There were zero short watches, short candidates rejected by filters, short admission blocks, and short submissions. The simulated account was flat on those 59 closes. The chart's last closed SELL card belonged to the **November 9 03:00 UTC** entry at 4120.008, whose last exit was at 12:45 UTC on November 9. [Screenshot](premium_v2_evidence/QIRVANTA_PREMIUM_V2_final_test_draft.jpg) · [Pinned native order journal](premium_v2_evidence/native_pair_20261110_1430Z_logs.zip)

S5 SELL is a failed upside breakout. With no existing setup, it requires a close above the highest high of the prior 20 bars, with the previous close at or below that same level and no gap between bars. That starts a watch at the closed bar and freezes its high as the anchor. A later bar must close below the frozen breakout level within three bars. A close above the frozen high cancels the watch before the reclaim check. A candidate that reclaims must still be bearish, have a body of at least 0.25 current ATR, and close in the bottom 35% of its range. The hard filters then test the cooldown, session, date, news pause, volatility, candle size, optional H1/FVG/premium filters, and executable plan geometry. The large bearish legs on M15 had no active SELL watch and never seeded the required upside breakout. The qualifying structural direction for these legs was down. [Pinned strategy](../unified/experimental/QIRVANTA_SNIPER_PREMIUM_V2.pine#L1142) · [Trace and per-bar decisions](v21_evidence/v21_decisions_and_native_fills.zip)

| M15 close UTC | Close | Prior 20-bar high | Prior 20-bar low | Range / current ATR | S5 SELL state |
|---|---:|---:|---:|---:|---|
| 00:30 | 4128.695 | 4148.710 | 4100.590 | 1.098 | No raw trigger |
| 02:45 | 4116.225 | 4148.455 | 4120.550 | 1.571 | No raw trigger |
| 09:15 | 4095.760 | 4128.010 | 4108.420 | 3.190 | No raw trigger |
| 13:45 | 4056.745 | 4118.730 | 4068.095 | 4.469 | No raw trigger |
| 14:45 | 4045.000 | 4090.975 | 4039.720 | 2.230 | No raw trigger |

The 09:15 and 13:45 bars also exceeded the 2.6 ATR entry-candle size cap. That was a recorded flag on each bar. The S5 code only reaches the cap as an actual rejection after a candidate reaches the raw reclaim step. With the default values, the other common plan thresholds are 0.55–2.8 current ATR/mean ATR, 0.8–3.5 ATR initial stop distance, a 0.12 ATR structural stop buffer, expected cost no greater than 18% of risk, and a cooldown of more than eight bars since confirmation on that side. Session, discretionary news pause and extra H1 agreement were off in the run. [Pinned inputs and gates](../unified/experimental/QIRVANTA_SNIPER_PREMIUM_V2.pine)

### What the same S5 did on M5

Of 178 November 10 M5 closes available by the screenshot, 174 had no raw SELL trigger. Two started a short watch. One of those watches cancelled, and the other led to the day's only S5 SELL.

| Close UTC | Observed decision |
|---|---|
| 05:25 | WATCH. Close 4121.120 exceeded the prior 20-bar high 4117.785. Frozen anchor was 4121.985. |
| 05:30 | Cancelled for structural invalidation. Close 4123.580 exceeded 4121.985. |
| 05:45 | WATCH. Close 4125.925 exceeded prior high 4124.120. Frozen anchor was 4128.010. |
| 05:50 | Reclaim at 4121.860 below 4124.120. S5 confirmed and the native RAW portfolio submitted three units with stop 4128.547. |
| 05:55 | The strategy observed the next-open native fill at 4121.893, effective 05:50 UTC. |
| 07:35 | Observed TP1 at 4115.173, effective 07:30 UTC. The remaining stop was scheduled at 4121.561. |
| 07:40 | Confirmed BUY was blocked while the two-unit short remained open. There was no SELL occupancy block. |
| 08:20 | Both remaining legs stopped at 4121.563, effective 08:15 UTC. Net campaign result was +6.330 USD on three units. |
| 10:40–11:00 | A separate S5 BUY was submitted at 10:40 UTC, filled on the next open at 4076.767 and stopped at 4072.753, effective 10:55 UTC. Net result was −13.092 USD. |

The M5 SELL arrived **330 minutes after** the first displaced prior-20-floor break at 00:20 UTC. Its remainder had already stopped before the strong M5 bars that closed at 09:05 and 13:35 UTC. Its native trade parts reported maximum run-up of 9.418 USD on the best still-open unit and maximum drawdown of 5.302 USD per unit in the observed emulator record. Neither native excursion is measured to the screenshot trough once the trade was flat. [Pinned M5 order journal](premium_v2_evidence/native_pair_20261110_1430Z_logs.zip) · [Native S5 trace](v21_evidence/v21_decisions_and_native_fills.zip)

### Calculation range and context

The 1,200-bar trace was warm on all 178 M5 and 59 M15 November 10 closes within the capture. The core HTF warmup predicate was true throughout. M15's additional H1/H4 research context was ready on all 59 bars. M5's additional H1/H4 quality context was not fully ready on any of the 178 bars. That context is distinct from the core HTF prerequisite and the optional H1 matching filter was off. M5 still confirmed and filled the early SELL.

The same trace with a 2,200-bar calculation setting aligned 240 exported M5 closes and 80 exported M15 closes with the 1,200-bar trace. No decision field or economic event payload changed. On M5 three closes contain different numeric suffixes for native exit events because the longer history contains 57 more earlier closed parts. Fill times, prices, stops, sizes, reasons, P&L and campaign IDs matched. The longer window still had M5's additional research context unavailable and left all 59 M15 SELL decisions at no raw trigger. This comparison covers the screenshot event. The extended observer ran with its visual tail removed and does not establish an uncapped Premium runtime result. [Matched traces and hashes](v21_evidence/v21_decisions_and_native_fills.zip)

## Event location and bearish information

The screenshot clock reads **14:52:12 UTC**. The exact price tape is native OANDA:XAUUSD standard candles exported from TradingView. In the fixed pre-roll from November 9 18:00 UTC through the screenshot's last confirmed bar, the highest high was **4148.710**, and the lowest low after that peak was **4033.915**, a drop of **114.795** price units or **2.767%**. Both frames agree on these extrema. The timestamps describe the source bar and when the bar was confirmed. The OHLC tape does not resolve the second when an intrabar extreme traded.

| Extreme | M5 bar UTC | M15 bar UTC |
|---|---|---|
| Peak 4148.710 | Nov 9 20:25–20:30 | Nov 9 20:15–20:30 |
| Trough 4033.915 | Nov 10 14:40–14:45 | Nov 10 14:30–14:45 |

The last M5 close available by the screenshot clock was 14:50 UTC at 4034.610. The rightmost M15 bar had opened at 14:45 UTC and was still forming. The lower **4033.030** value is established by the later-completed M5 14:50–14:55 bar, also included in the 15:00 M15 close. The timing within that bar remains unverified. The outcomes below exclude it. [Aligned decision timelines and hashes](v21_evidence/v21_decisions_and_native_fills.zip)

V2 Intelligence identified bearish information earlier through its own confirmed structure. On M5, the first observed short after the peak was a failed breakout at **November 9 20:45 UTC** at 4137.835, followed by a short MSS at **21:45 UTC** at 4135.255. Both were low-confluence observations. Its first November 10 M5 short was a low-confluence MSS at **00:05 UTC** at 4136.050. On M15 the first was an accepted moderate-confluence MSS at **00:30 UTC** at 4128.695. It then accepted a low-confluence BOS at **02:45 UTC** at 4116.225. The standalone indicator evaluates prior-known swing levels at confirmed close and installs newly confirmed pivots after that evaluation. Its acceptance controls research labels and carries no order authority. [Pinned Intelligence indicator](../unified/experimental/QIRVANTA_SNIPER_V2_INTELLIGENCE.pine) · [Native candidates and watches](v21_evidence/v21_decisions_and_native_fills.zip)

On November 10 through the screenshot clock, native Intelligence reported 26 candidates on M5 with 18 accepted overall, including 11 accepted short and eight rejected short observations. On M15 it reported seven candidates with five accepted overall, including two accepted short and two rejected short observations. The two M15 rejections were 09:15 and 13:45. Initial structural distance measured **4.819** and **9.581 prior ATR**, versus the **4.0 ATR** cap. Absolute close-to-EMA21 distance measured **3.181** and **3.770 prior ATR**, versus the **3.0 ATR** cap. Extra hard gates for joint H1/H4 trend and structural room were disabled in this run. The V2 S5 chart uses the native S5 plan state and does not read these separate observations. [Native Intelligence logs and configuration](v21_evidence/v21_decisions_and_native_fills.zip)

## Next-tick price opportunity and risk

The diagnostic breakout marker is the first bearish close crossing below the prior 20 completed bars' lows. For the displacement variant, the body must reach one prior ATR and the close must finish in the lower quartile. The first qualifying bar on each frame also met displacement. A crossing is known only at the confirming close. The benchmark scenario takes the contiguous next bar open with two adverse 0.001 ticks, places a stop above the high of the three bars known at confirmation plus 0.10 prior ATR, and tests a 2R limit target. A bar that hits both exits is resolved stop first. Baseline commission is 0.175 USD per fill per unit. The stress adds 0.35 price units of round-trip spread, separate from native slippage and commission. The screenshot's eventual trough is used only to measure the post-confirmation path.

| Measure | M5 | M15 |
|---|---:|---:|
| First displaced floor break UTC | 00:20 | 02:45 |
| Prior 20-bar floor / confirming close | 4133.550 / 4128.565 | 4120.550 / 4116.225 |
| Next open / modeled short fill | 4128.725 / 4128.723 | 4116.275 / 4116.273 |
| Stop / initial risk | 4139.565 / 10.842 | 4128.958 / 12.685 |
| Full path maximum adverse excursion to cutoff | 12.732 | 11.737 |
| Full path maximum favorable excursion to cutoff | 94.808 | 82.358 |
| Favorable excursion through modeled exit, conservative | 7.093 | 25.370 |
| Modeled exit and first known exit-bar close UTC | Stop / 01:30 | 2R target / 09:15 |
| Modeled net USD per unit, baseline / stress | −11.194 / −11.544 | +25.020 / +24.670 |

The two breakout benchmarks are event diagnostics. They use one fixed stop and exit geometry applied identically to both frames and are separate from S5 and the V2.1 impulse rules. They show the sensitivity of feasible capture to when information arrived and how the adverse path evolved. The earlier M5 benchmark would have been stopped well before the strongest decline. The scenario uses bar prices and cannot recover the historical broker spread, queue, or intrabar execution sequence. [Machine-readable outcomes and per-bar logs](v21_evidence/v21_decisions_and_native_fills.zip)

### The new impulse engine on the same event

M5 produced a SELL WATCH at 00:20 and confirmed continuation at 01:00. It rejected entry because the proposed stop was 2.679 prior ATR away, above the 2.50 ATR cap. The next SELL WATCH at 02:35 led to an entry plan at 02:40 with stop 4128.544 and target 4087.882. The native emulator filled three units at 4114.978 on the next open, then exited by time at 04:40 at 4116.032, for **−4.212 USD net**. A later 03:50 candidate was blocked by this position. The late 13:35 watch expired at 14:20 without confirmation. [V2.1 strategy](../unified/experimental/QIRVANTA_SNIPER_V21_IMPULSE.pine) · [Native event records](v21_evidence/v21_decisions_and_native_fills.zip)

M15's 02:45 watch expired at 05:00. Four subsequent short confirmations at 09:30, 10:15, 11:00 and 14:00 were rejected by the 2.50 prior ATR EMA extension cap. At 09:30 and 14:00 the stop also exceeded the 2.50 prior ATR risk cap. There was no M15 impulse entry during the decline. The screenshot event by itself offers no basis to relax these caps or promote the experiment. [Native event records](v21_evidence/v21_decisions_and_native_fills.zip)

## Reproduction

Generate the disposable S5 trace from the pinned V2 source with `python unified/tools/build_missed_sell_trace.py s5_trace.txt`. The temporary trace defaults to S5, standard OANDA candles and 1,200 bars. Change the calculation setting to 2,200 for the longer comparison. The trace emits no additional orders and does not need a private saved script.

The original price-bearing exports and complete timelines are retained in local `outputs/QIRVANTA_V21_missed_sell_private_audit.zip`. Extract that private audit, then run `python unified/tools/audit_missed_sell.py --logs-dir path/to/raw --output-dir path/to/audit`. The CSV timelines contain each bar's exact OHLC, S5 states and reasons on both sides, raw events, occupancy, warmup and context, and separate Intelligence candidates and watch transitions. The JSON carries source file hashes and compares the calculation ranges. The archive's V2.1 extracts preserve the contemporaneous native event rows from the separate impulse test. The previous release native order journals remain separately pinned. [Published decision projection, native fills and original hashes](v21_evidence/v21_decisions_and_native_fills.zip)
