# QIRVANTA GOLD PRO — PRECISION BUY/SELL ZONES
## ONE FOCUSED CODEX MISSION · IMPLEMENTATION AND VALIDATION

**Repository:** dani12mz-ui/QIRVANTA-SNIPER
**Protected baseline:** 5528556584910faa75f1b76d2d96f14d3852e6e7
**Work branch:** research/qirvanta-precision-buy-sell-zones
**Base Pine:** research/final_htf_data_integrity/QIRVANTA_GOLD_PRO_HTF_DISPLAY_FIX.pine

## MISSION

Deliver the most precise defensible BUY demand zones and SELL supply zones that the available price data can support, with clear confirmation and invalidation on OANDA:XAUUSD M5/M15/M30. The user wants practical **price ranges on chart**, not more retrospective arrows. A displayed zone is not a guaranteed reversal, and no study to date proves profitable expectancy. Do not claim otherwise.

Keep the verified S5/RC2 entry engine, orders, alerts and P&L completely unchanged. The new zones must first be **observation only**. Do not add a sprawling new trading strategy. Favor minimal additions and performant Pine.

## 1. FIX ZONE RULES BEFORE SEEING OUTCOMES

Choose one small, fully specified zone-finding rule with mirrored BUY/SELL criteria, then document all defaults in a machine-readable registration file before testing. Use existing confirmed market-structure pivots, displacement, and FVG evidence where available. If a field can only be calculated later, zone creation must occur on its true known-at close, not when the origin candle formed.

Suggested **research specification**, not proven optimal:

- BUY origin: last bearish candle immediately preceding a confirmed bullish structural break with directional displacement >= 0.50 ATR(14) using the reference ATR known at the event close. SELL: last bullish candle before mirrored bearish break.
- Search origin within the preceding 1–5 **closed** bars. Use fixed deterministic tie-breaking: nearest qualifying origin, then lowest/highest bound. Never choose the best historical performer.
- Proximal edge: origin candle body edge facing present price; distal edge: origin candle wick extremum. Document buy/sell exact formula and require strictly positive box height.
- Skip if zone height exceeds 2.0 ATR(14), is below two ticks, or origin data unavailable.
- Optional 3-candle FVG *confluence marker* is recorded only when three required bars have closed. Absence of FVG does not automatically invalidate the zone, and no use of future FVG candles to backdate zone.
- Freeze high/low, origin, known time, zone ID and first-break evidence at detection. Use only known-at confirmed pivots. No repainting or drawing early markers at origin as though zone were known then.
- At most two recent active BUY zones and two recent active SELL zones in each selected timeframe. Reuse Pine box/line objects. If more opportunities exist, rank by freshest causally qualified event, not post-event success.

## 2. ZONE LIFE CYCLE

States:
FORMATION DETECTED (confirmed known-at)
UNTESTED
FIRST TOUCH
REJECTION CONFIRMED
PARTIALLY MITIGATED
FULLY MITIGATED
BROKEN / INVALIDATED
EXPIRED

- Touch means price high/low intersects already known zone. Tag if intrabar observation; confirmed version only at candle close.
- Rejection confirmed only on a subsequent or same fully closed candle showing close outside the proximal edge in the proposed direction and body >= 0.25 ATR(14), no gap/lookahead.
- Invalidate a BUY if a confirmed Close is below distal boundary by >= 0.10 ATR; SELL if confirmed Close is above distal by >= 0.10 ATR. Clearly distinguish touch/wick sweep from confirmed invalidation.
- Expire after max 24 chart bars since detection, regardless of touch status. Do not relabel expired historical zones as currently active.
- A broken supply zone turning into potential demand (or inverse) must be a NEW hypothesis with NEW causal confirmation; never silently flip direction.
- If S5/RC2 shows opposite READY, explicitly show CONFLICT, never hide it.

## 3. VISUAL REQUIREMENTS

Keep existing premium table and pressure meter. Add an optional compact section **ACTIVE BUY ZONE / ACTIVE SELL ZONE** with exact proximal–distal ranges and status/UTC age. The chart should render:
- BUY: refined translucent emerald band, thin solid borders, small right-edge label `BUY ZONE · WATCH`.
- SELL: translucent muted red band, equivalent `SELL ZONE · WATCH`.
- First touch: subtle dot or tick; confirmed rejection: `BUY REJECTION` or `SELL REJECTION` compact tag; invalidated/expired: faded or removed.
- Optional zone-quality note: ALIGNED / MIXED / HTF DATA MISSING based ONLY on *verified available* HTF snapshot. Do not treat missing as MIXED or as permission.
- No arbitrary percentages such as `90% BUY`. Pressure remains an OHLC/tick-volume proxy.
- When and only when S5 READY exists, retain original approved Entry, SL and TP1–TP3. If a research-only zone has no actual validated executable plan, DO NOT fabricate Entry/SL/TP.
- Larger font by one native Pine size step where feasible, configurable Desktop/Compact/Mobile. No candle occlusion; chart remains understandable without visual overload.

## 4. EARLY AND CONFIRMED ALERTS

Only add *optional and clearly labeled* zone observations:
`RESEARCH BUY ZONE TOUCHED`, `RESEARCH SELL ZONE TOUCHED`,
`RESEARCH BUY REJECTION CONFIRMED`, `RESEARCH SELL REJECTION CONFIRMED`, `RESEARCH ZONE INVALIDATED`.
Confirmed signals appear only on full bar close, event-id deduplication. Keep all existing S5/RC2 alerts and their payloads byte-for-byte. No broker orders.
If user account has zero technical alerts, document blocked push delivery, don't purchase anything.

## 5. EMPIRICAL TEST

Before incorporating to release, perform full Pine/Python event-parity tests with identical OANDA:XAUUSD history, exact warmup and native M5/M15/M30 plots.
Metrics must include:
- zones detected; first-touch rate; rejection confirmation rate; invalidation and expiration;
- false reversal rate after confirmation in predeclared 3/6/12-bar horizons;
- time from formation-known to first touch; from touch to rejection confirmation;
- fraction of significant directional moves covered *before* their main displacement, using independent registered labeling;
- whether observing zone led to a genuinely available, cost-adjusted executable opportunity; not merely touching TP afterward;
- negative examples: trend continuation breaking zones, sudden news candles, range chop, missing HTF and flat/zero-range bars.
- Exploratory shadow next-open entry and structural stop only as independent, clearly non-promoted research, cost/stress sensitivity with position occupancy and stop-first OHLC ambiguity. Do NOT route to S5 READY or modify live trade behavior.
- Outcomes on known history are exploratory. Use pre-registered untouched future data for real selection, without contaminating sealed GOLD PRO forward 2026-11-16–2027-01-11 and other committed holdouts.

## 6. STRICT TECHNICAL ACCEPTANCE

Pine v6 compiler has previously timed out on bigger combined scripts. Before pushing full code:
- Prefer existing pivots/FVG/ATR/structure variables. Avoid new request.security calls, duplicate data structures, expensive loops, and increasing chart object limits.
- First try one low-complexity prototype with **zone drawing only**, then add lifecycle and QA only if compilation remains stable.
- Stop immediately on repeat compile-time timeouts and keep previously verified indicator unchanged. Separate optional zone companion if needed, subject to actual TradingView Basic two-indicator capacity.
- Native compile/add-to-chart OANDA:XAUUSD M5/M15/M30.
- Exact parity for baseline CORE/READY, Entry, SL, TP, native paper fills, S5/RC2 payloads and P&L. Retain all source timestamps, compare on matched history and same loaded start. No HTF lookahead.
- Real screenshots: active BUY zone, active SELL zone, confirmed rejection, invalidation and mobile 390px if observed. Never mock up a live signal.
- Test both light/dark and readable large-font UI.
- Produce Bulgarian final report with VERIFIED, EXPERIMENTAL, INCONCLUSIVE or BLOCKED labels, one experimental Pine file only if compiled, source/logic tests, native QA and a single new commit.

## DEFINITION OF SUCCESS

Success means precise **causally anchored zones** with readable prices and clear states, without altering protected trading performance. A newly profitable trade model has NOT been demonstrated merely by detecting more zones. Never promise best or guaranteed signals. Deliver the smallest usable result and stop endless architecture changes.
