# QIRVANTA S5 — COMPILER RESCUE (ONE-FILE GOAL)
Status: SPEC ONLY. Neither a slim core nor a combined version is native-verified yet.

Baseline: commit 5528556584910faa75f1b76d2d96f14d3852e6e7
Source: research/final_htf_data_integrity/QIRVANTA_GOLD_PRO_HTF_DISPLAY_FIX.pine
Zone reference: commit 2a4d2e3daee64e7b73e2f3a95fce6a1762aa3d07
Failed all-in-one: feature/qirvanta-gold-unified-luminous
Failed Lite: commit 3e7803806a382afcffa79ba4f114fd5018b19c15
Working research branch: research/qirvanta-s5-compile-rescue

## Root problem
Adding even an independent 114-line zone layer to the unpruned ~3594-line UNIFIED causes a TradingView compilation timeout. Static line counts, source-token reductions and successful unit tests cannot establish that the Pine compiler will accept the result. We must **not** keep submitting near-identical combined scripts.

The original UNIFIED has tvStrategyMode hardcoded to "S5" but contains many legacy alternative strategy families S0–S10 and F0–F10, management variants disabled by constant flags, unused QA and native execution helpers. These are candidates for **dependency-guided elimination**, not blanket deletion. Trimming visible text alone is not enough. S5 CORE, risk, permission, plot/alerts, lifecycle, price rounding and existing RAW PAPER behavior are reference outputs, not negotiable.

## One integrated Codex mission
1. Begin from verified baseline 5528556. Build an actual call/dependency graph for the executed S5 configuration and every user-visible function. Use constant propagation where valid; do not assume Pine eliminates unreachable branches by itself.
2. Distinguish immutable S5 behavior from disabled experimental families, F variants, manual management, other books, unused QA dumps, and duplicated presentation helpers. Produce a dead-code removal inventory with proof that removed paths cannot affect S5 state, initialization, historical calculations or any displayed/alerted output.
3. Save baseline SHA-256 and archived native exports. Freeze comparison windows, symbol, OHLC, start bar, warmup, settings, chart type, and complete event fields **before** editing.
4. Make a standalone **S5 ONLY SLIM** indicator that keeps existing S5 WATCH/CONFIRMED/READY, risk, Entry, SL, TP1–TP3, filled/closed RAW PAPER P&L, Directional Observation/Shadow, Early Watch, Pressure Meter, H4 DATA MISSING, all current alerts, and premium dashboard. Do not add zones yet.
5. Static-check the slim version for type/scope and other Pine errors. When a substantial compiler-complexity reduction has actually been achieved, do **one guarded native compilation** on test TradingView M5. If timeouts persist, STOP rather than deplete remaining attempts. Never overwrite user saved scripts.
6. If S5 ONLY SLIM compiles, compare all available native logs against baseline on exact OHLC/warmup; not only presence of markers. Verify CORE, READY, plan geometry, risk rejection, alert payload/event IDs, simulated entry/exit, position occupancy, net R/P&L and drawdown. Check H4 missing vs mixed and historical review. No unexplained differences are acceptable.
7. **Only after slim baseline passes**, add a minimally integrated luminous zone overlay without a second dashboard or duplicated HTF requests. The zone origin/known-at/price boundaries and lifecycle should match the standalone zone research where feasible. If weaker Lite logic is used, it must be visibly labeled research and tested as a distinct method.
8. Compile combined S5 + Zones natively only after static tests and a meaningful complexity budget exists; guard compiler attempts. Test M5/M15/M30, light/dark, 390px viewport, active BUY/SELL zone, rejection/invalidation, plan labels not obscured.
9. Compare S5 outputs AGAIN to original baseline, and independently compare zone events to 2a4d2e3, recording any differences. Do not change S5 risk gates, retune profit targets or use sealed forward data.
10. Deliver **one** final Pine source if and only if native VERIFIED. Include Bulgarian QA, actual screenshots, hash of final Pine and complete reproducible parity results. If blocking persists, output BLOCKED, retain the two verified indicators as the practical solution. Do not create a fake release or demand successive 'continue' prompts.

## Strict non-goals
- No new strategies; no claimed positive expectancy; no live trading authorization.
- No compiler retries without meaningful structural changes.
- No modification of main, S5/RC2 baselines or sealed forward test.
- No claim that smaller source text means it compiles.
