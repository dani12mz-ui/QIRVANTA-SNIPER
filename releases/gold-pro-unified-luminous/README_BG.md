# QIRVANTA GOLD PRO — ALL IN ONE (UNIFIED + LUMINOUS ZONES)

**Status: merged source, native TradingView compilation not yet verified.**

This branch includes one complete Pine v6 file, constructed from two individually verified sources:

- Main S5/RC2 UNIFIED, commit 5528556: market context, Pressure Meter, directional observations, READY, Entry, SL and TP1–TP3.
- Luminous Precision Zones, commit 2a4d2e3: BUY/SELL zones, luminous rails, rejection and invalidation markers, chart labels and separate zone dashboard.

[Download/view the complete combined Pine file](QIRVANTA_GOLD_PRO_ALL_IN_ONE.pine)

## Source-level checks completed

- Exactly one version directive and one indicator declaration.
- The original main Pine body is unchanged after its indicator declaration.
- The original Luminous module body is unchanged after its indicator declaration.
- Indicator object limits raised to 64 labels, 26 lines and 12 boxes; calc_bars_count stays 1200.
- Original S5/RC2 trading logic remains unmodified; zone events are research-only, not READY signals.
- Main and sealed forward test remain untouched.

## Required native test

1. Open only the combined Pine in TradingView Pine Editor (OANDA:XAUUSD, standard candles, M5/M15/M30). Do not overwrite the saved working version.
2. Compile and add to chart. Past large combined candidates have hit Pine compiler timeouts; do not repeatedly retry into account lockout.
3. If compiled, match original S5/RC2 signal, Entry/SL/TP, alert and paper P&L streams against 5528556 on identical OHLC/warmup.
4. Separately compare zone ID, known-at timestamps, proximal/distal boundaries, touch/rejection/invalidation and alerts against 2a4d2e3.
5. Inspect both dashboards simultaneously (main upper right, zone lower left), dark/light, active buy/sell, M5/M15/M30 and 390px mobile. Check labels for clipping.
6. Fix only validated compile/render issues in this branch. If the combined script times out, keep both known-working source files as the fallback.
7. Publish an evidence-based QA report; do not claim VERIFIED without native screenshots and parity evidence.

## Suggested settings

- Main display PREMIUM, upper right; historical review OFF for live chart.
- Luminous Zones ON, Luminous borders ON, formation badges ON, zone projection 3 bars.
- Zone panel DESKTOP at bottom left; MOBILE preset at 390px; reduce projection to 1 bar if cut off.

**Risk:** Precision Zones are observation-only and previously tested modeled entries were loss-making after stressed costs. This merged build does not establish profit or authorize live orders.