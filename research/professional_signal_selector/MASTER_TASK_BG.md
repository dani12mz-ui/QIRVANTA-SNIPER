# QIRVANTA GOLD PRO — PROFESSIONAL SIGNAL SELECTOR
## Една ограничена задача за Codex · FROZEN RESEARCH SPECIFICATION

**Статус:** SPECIFICATION ONLY — не е нов проверен Pine индикатор, няма доказана доходност. Нищо от този документ не разрешава реални сделки.

**Repo:** `dani12mz-ui/QIRVANTA-SNIPER`
**Изходна версия (pin):** `192986d5fd5dddbaead0f7a45a8075b9137082f7`
**Research branch:** `research/qirvanta-professional-signal-selector`
**Main Pine:** `research/signal_quality_reliability/QIRVANTA_GOLD_PRO_SIGNAL_QUALITY.pine`

## Защо

Последният нативно проверен QIRVANTA вече съдържа S5/RC2 READY, ранни наблюдения, direct-trend shadow, H1/H4 bias, market regime, signal freshness, wick observations, estimated pressure и reasons. Проблемът НЕ е липсващи индикатори. Нужна е проверима политика за **приоритизиране на наблюдения**, която да различава предимство от несигурност и да не повишава непроверени наблюдения до поръчки.

Исторически наблюдения — **не са доказателство за печалба:** M5 Shadow 46/82 false breakouts; допълнителна потвърдена свещ 22/58, +84.00 USD базово, −45.37 USD стрес, средно R отрицателно. Използването на тези стойности за настройване на прагове замърсява архива. 11 ноември SELL с недостатъчно room 0.262R vs 0.70R е показателен, но НЕ е основание да се сваля защитния праг.

## Цел

Изгради **един** прозрачен, по време причинен и лек `Professional Signal Selector` като опционален изследователски слой върху работещия UNIFIED; оригиналните CORE, READY, Entry/SL/TP, execution, alerts, calculated costs и P&L остават битово/събитийно непроменени. Не добавяй нов trading engine. В бъдеще разрешение за реален вход е възможно само след външен независим тест и отделно решение.

### I. Freeze before coding

Запиши `PROTOCOL.json` с pinned hashes, matching OHLC windows, signal definitions, candidate count, fixed thresholds, outcome metrics и неизползван период за нови данни. Не разглеждай sealed original GOLD PRO forward period 2026-11-16 to 2027-01-11 за селекция на параметри. Precision forward от 2027-01-12 е отделен. Ако няма чиста достатъчна извадка, резултатът е **INCONCLUSIVE**, а не преоптимизиран backtest.

### II. Selector — изисквания

За всеки вече наличен observation event изведи:
- `event_id`, symbol, timeframe, direction, event/model source, `known_at_utc`, expiry, state, research/approved classification.
- Confirmed close vs provisional: само окончателно затворен бар може да даде `CONFIRMED`.
- HTF alignment: последните известни/затворени H1 и H4, разделно `ALIGNED`, `MIXED`, `COUNTERTREND`, `DATA MISSING`.
- Structure confirmation: frozen, previously known support/resistance and close relative to it; false-breakout reclaim is assessed only when it becomes known.
- Momentum/body displacement and volatility measured **at event confirmation**, not recomputed from later bars; ATR and EMA use prior/confirmed data.
- Freshness/expiry: reject stale and duplicate candidates, never redisplay expired historical events as current.
- Net structural room and proposed risk when and only when a valid causal plan exists. Missing geometry -> `NO EXECUTABLE PLAN`, not invented prices.
- Cost burden from verified or explicitly assumed spread/slippage/commission; `BROKER COSTS UNKNOWN` when necessary.
- Conflicts and occupied portfolio slot, with source of each side.
- If a field is unavailable, mark `UNKNOWN` and do not silently award points.

Produce transparent **qualitative triage**, not a fake success probability or fabricated 90%-accurate confidence:
- `RESEARCH PRIORITY`: supported confirmed directional observation with all specified evidence checks known and passed, still **NOT TRADE APPROVED**.
- `OBSERVE`: early watch/weak or mixed evidence.
- `CONFLICT`: competing confirmed observations or unresolved opposing context.
- `REJECT / EXPIRED`: source invalidation, known risk failure or stale lifecycle.
- `NO DATA`: incomplete evidence.

Use at most **three pre-registered** candidate selector policies and one unchanged baseline. Choose thresholds before any test; do not combine dozens of indicators to improve the score in retrospect. Avoid simplistic "3 of 5 = 80% accuracy". For S5 READY add separately `S5 APPROVED`; selector may annotate quality but may NOT suppress, create, change or execute a S5 plan.

### III. Evaluation (mandatory, not cosmetic)

Use the repository's existing native logs and Python replay. Evaluate quality as a **ranking/policy over opportunities**, preserving whether they were approved, rejected, blocked by occupancy, or only observed. Do not count a post-hoc profitable rejected signal as an executed win.

Compare each selected policy to unmodified S5/RC2 and to its parent source using:
1. False-positive rate using predeclared 3-bar reclaim rule, plus denominator and uncertainty.
2. Directional opportunity capture, detection latency and executable entry availability. Backtest both missed BUY and missed SELL events.
3. Excluded winners, avoided losses, and changed occupancy on a common execution ledger.
4. Net R, net USD, Profit Factor, mark-to-market drawdown, tail loss, win/loss ratio.
5. Normal and stressed **bid/ask** costs, realistic next-open fills, gaps, same-bar stop-first conservative ambiguity; do not double-count spread.
6. No-lookahead and timestamp audit at every source event; bar-prefix invariance and MTF warmup equivalence.
7. Multiple market regimes; horizontal/BOS false breakout cases; chronological split; uncertainty and multiple comparisons. If holdout unavailable, say so.

Preserve failed candidates and all unfavorable outcomes.

### IV. Pine implementation and performance

**Compiler risk:** verified baseline is about 3,579 lines and already complex; prior 3,688-line experiment timed out. Do not add another event engine, array copying, extra `request.security`, independent chart table, or loops over long history.

Implement the smallest possible **feature-flagged display-only** patch using existing signal snapshots and decision fields. Store quality evidence when it becomes known, not with future price; if true frozen evidence cannot be exposed cheaply, implement the selector and event QA in Python first and leave Pine presentation unchanged rather than guessing.

- One compact `QUALITY / WHY` explanation in the existing PREMIUM dashboard, optionally next to `DIRECTIONAL OPPORTUNITY`.
- Display `RESEARCH PRIORITY` clearly *distinct from* `BUY READY/SELL READY`.
- Existing pressure remains `PROXY`, not real buyer/seller market-order flow.
- Preserve premium chart labels, signal price coordinates, desktop/mobile readability.
- No new implied TP/SL for observation-only events.
- No change to existing `alert()` payloads or tradable conditions. Optional research-only alert needs explicit opt-in, deduplication and real confirmed close.
- If a meaningful improvement requires exceeding Pine compilation bounds, keep the tested baseline and create an **external QA dashboard/report**, not a huge uncompiled replacement.

### V. Acceptance

1. Native compile/add-to-chart on OANDA:XAUUSD M5/M15/M30 standard candles. If timeout, stop before TradingView lockout and retain baseline.
2. Identical S5/RC2 CORE/READY, Entry/SL/TP, alerts, RAW PAPER and P&L on matched dates with identical warmup; exact event parity.
3. Tests for observation vs permission disagreement, long bearish trend with countertrend BUY WATCH, false breakouts, mixed H1/H4, missing volume, late expiry, historical review, and retained source timestamps.
4. Native screenshots (real) M5/M15/M30, dark/light and mobile 390 px.
5. Audit existing TradingView Basic alert limitations; never claim iPhone delivery without observed push.
6. Publish **one** experimental Pine candidate if compiled, full Python evaluation, protocol, Bulgarian report, before/after data and one commit. Do not publish fake "BEST STRATEGY" or claim profitability.

### VI. Promotion rule

A candidate **cannot** replace S5, reduce risk gates or become executable unless it beats the unchanged baseline on **untouched** data by positive net expectancy after stressed costs, without greater drawdown or worse tail risk. It also must have enough campaigns across market regimes. If not: `RESEARCH ONLY`; keep existing stable indicator. No live broker orders.

**WORK ORDER:** research audit → freeze policy → source-level minimal implementation → static tests → *one* cautious native compilation → parity → out-of-sample evidence → final report. Finish all accessible steps in one run; do not ask for ordinary "continue" prompts. Report all genuine blockers.
