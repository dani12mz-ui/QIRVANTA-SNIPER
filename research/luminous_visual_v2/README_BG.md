# QIRVANTA GOLD PRO — VISUAL SIGNATURE V2

**STATUS: SOURCE-LEVEL CHECKS PASS. NATIVE TRADINGVIEW COMPILE / VISUAL QA PENDING.**

This is the original native-verified single-file S5 + Luminous from commit `26ad1aa2992f9e2e7e28c13fbbd3f321e1de8261` with **presentation-only changes**. No new strategy and no altered trade levels.

## Real visual changes in the published code

- Desktop dashboard text defaults to **Large**, with larger base text, headings, metadata and entry/SL/TP prices. The existing Small and Standard choices remain available.
- Full dashboard palette adjusted to darker graphite, richer petrol green/salmon red and brighter white/gold/cyan labels.
- BUY/SELL confirmed chart markers now use `normal` size when Large on desktop. Passive watch markers use `small` instead of `tiny`.
- Plan Entry, SL, TP1/TP2/TP3 right-edge price badges now use larger text, opaque graphite surfaces and high-contrast colors, with heavier Entry/SL/TP3 rails.
- Wick observational zone text is enlarged; its band/border are more visible.
- Luminous BUY/SELL bands have stronger color, a brighter band edge, wider translucent halos and a more apparent accent layer.
- Zone price badge now includes `BUY / DEMAND` or `SELL / SUPPLY`, status and the actual price range, with larger labels.
- `Show BUY / SELL research zones` also controls research zone event markers.
- All zone events remain **RESEARCH ONLY**, even if rejected/confirmed; nothing in a zone becomes S5 READY.

## Verified statically (not native)

- Protected S5/RC2 source before the presentation input remains identical to the committed baseline.
- Pine block `if lzShow and rcViewPaintBar` (zone formation/touch/rejection/invalidation/expiry) is byte-identical to baseline.
- Exact source has one Pine v6 `indicator()` and no new `request.security`, `strategy.entry`, or `alert()` invocation.
- No change to S5/RC2 plan generation, matching, Entry/SL/TP geometry, alert payload construction, RAW PAPER accounting or market data dependencies.
- Static parity cannot prove native executable parity or rendering.

## Mandatory one-run Codex QA

1. Review the exact file `research/luminous_visual_v2/QIRVANTA_GOLD_PRO_VISUAL_V2.pine` in branch `feature/qirvanta-gold-luminous-visual-v2`.
2. Perform a Pine v6 static compile-readiness check of `line.set_width`, `label.set_size`, text-size expressions, color branches, slot indices and drawing object limits.
3. Compile in a fresh authorized TradingView test tab once, `OANDA:XAUUSD` M5 standard candles. If successful, switch to M15/M30. Do not repeatedly hit compile when timeout appears.
4. Compare with the pinned `26ad1aa` control on matched chart history: CORE, READY, source ID, Entry, SL, TP1–TP3, rc2 alerts, simul. entries/exits, position capacity, RAW PAPER P&L, costs and drawdown.
5. Before/after actual browser screenshots: M5 active BUY and SELL, plan Entry/SL/TP, zones with rejection, light/dark, desktop wide and iPhone 390x844 MOBILE.
6. Focus on **readability**: ensure 1) BUY/SELL label and time legible at 100% browser zoom; 2) zone price readable without tooltip; 3) no overlap hiding price bars; 4) dashboard doesn't obscure key candles; 5) light canvas has usable contrast.
7. If Large default is too wide on mobile, adjust ONLY mobile-specific typography. Chart Mobile mode must fit. If labels collide on M5, stagger only drawing y/offsets while preserving price levels and known-at timestamps. No shift of event time.
8. Only optimize or change UI style code. If still visually weak, do one evidence-driven refinement and repeat native checks. Do not add extra heavy indicator models.
9. Publish a real `NATIVE_QA_BG.md`, actual screenshots and one QA commit on this branch. Label BLOCKED where not checked. Preserve `main`, protected S5/RC2 and forward seal.

## Usage

- Desktop: `GOLD PRO RC2 → Изглед PREMIUM`, `PREMIUM · ГРАФИКА → Размер на таблото Large`. The new Large default is active in this candidate.
- Phone: `Изглед MOBILE`; if zone labels clip, use right margin or shorten projection to 1 bar.
- Luminous: `Show BUY / SELL research zones ON`, `Luminous zone boundaries ON`, `Right projection 2 bars`.
- For real-time monitoring `Исторически преглед OFF`. S5 READY plan is not a promise of a profitable trade.
- Paid effects cannot use CSS shadows/true glow in Pine; the halo is a wide translucent native line.

**Important:** This code is a visual candidate — the original `26ad1aa` stays the known successful native compile until the new file passes the same acceptance checks.
