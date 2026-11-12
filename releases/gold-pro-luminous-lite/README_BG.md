# QIRVANTA GOLD PRO — SINGLE-FILE LUMINOUS LITE

**STATUS: CANDIDATE — not yet compiled natively in TradingView.**

[Complete Pine v6 source](QIRVANTA_GOLD_PRO_LUMINOUS_LITE.pine)

This is a lighter response to the `Pine compilation was timed out` error from the previously attempted 3978-line all-in-one file.

## What it contains

- Original complete S5/RC2 UNIFIED + Pressure Meter, Directional Shadow, H4 DATA MISSING display, Entry/SL/TP, existing alerts, and main dashboard from commit `5528556`.
- Single inline Luminous zone presentation using previously confirmed ATR, bullish/bearish candles and completed market pivots. It detects BUY demand and SELL supply origin candles, tracks touch/rejection and expires after 24 bars or confirmed structural break.
- Double luminous border rails and two-layer fill, price labels with exact boundaries, optional research-only rejection/invalidated badges.
- Only one indicator declaration: the Lite zone layer does not introduce an independent full research engine or Pine request call.

## Explicit reductions and caveats

- This does **not** reproduce the standalone Luminous Zones companion in full. It removes the separate precision zone JSON alerts, QA streams, FVG metadata and full partial/full mitigation labeling to reduce compiler burden. Zone signals are approximate visual research observations. The complete zone event parity with `2a4d2e3` is **not claimed**.
- Original full S5/RC2 source body is preserved except `indicator()` declaration and object quotas. No core trade logic changes are intended.
- The new file has **not** yet passed native Pine compilation or parity. The previously successful sources still provide the tested fallback.
- The user has only a few remaining compiler attempts. **Do not submit several variants in sequence**, and do not repeatedly compile after another timeout.

## A single controlled native QA attempt

1. Have Codex statically check the entire Lite file for Pine v6 syntax/type errors, visual object limits, structural zone calculations and no modification of S5 decision code.
2. Load this exact Lite candidate in a temporary TradingView tab on `OANDA:XAUUSD` M5 with standard candles. Use the account's compiler budget cautiously; DO NOT overwrite the currently working private script.
3. If and only if M5 compiles successfully, test M15 and M30. Verify actual luminous boxes and labels, dark/light and mobile 390px.
4. On matched OHLC/warmup, compare S5/RC2 CORE, READY, plans, Entry/SL/TP, alerts, RAW PAPER entries/exits and P&L with the unchanged `5528556` baseline.
5. Audit the Lite zone signal known-at timestamps independently. Compare visual zones to standalone Precision Zones; log any differences rather than claiming parity.
6. Check historical review, missing H4, overlap, expiry, hidden-zone mode, mobile right-edge clipping. If visuals overlap, adjust only renderer and keep S5 unchanged.
7. If compilation times out: STOP. Mark this candidate `BLOCKED` and use the two independently verified scripts instead. Don't burn the remaining compiler allowance.
8. Publish factual QA and native screenshots, clearly marking VERIFIED/BLOCKED/INCONCLUSIVE. No guaranteed returns or live trading.

## Basic settings

- Main QIRVANTA: PREMIUM. Luminous zones are drawn directly on the price pane, no second dashboard.
- `LUMINOUS ZONES · RESEARCH`: Show ON, double boundaries ON, right projection 2 bars (1 on narrow mobile), visual rejection labels ON.
- Both original S5 approved orders and zones keep separate semantics: visual zones NEVER authorize S5 entries.

**Source references:** Verified UNIFIED `5528556584910faa75f1b76d2d96f14d3852e6e7`; separately verified full Luminous Zones `2a4d2e3daee64e7b73e2f3a95fce6a1762aa3d07`. Neither protected source nor the sealed forward test was modified.