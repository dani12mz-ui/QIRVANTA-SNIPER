# V2.1 impulse engine

**EXPERIMENTAL.** The impulse strategy runs in its own TradingView Strategy Report. Its directional rules and risk defaults were fixed before reviewing the example or evaluating the archive. The model supports standard OANDA:XAUUSD M5, M15 and M30 candles and begins decisions after 250 preceding chart bars.

| State | Information available on the current close |
|---|---|
| BUY / SELL WATCH | Close is strictly above / below the high / low of the prior 20 bars. The break level, close and bar index become the watch anchors. |
| SETUP EXPIRED | Close strictly reclaims the break level, or more than eight bars have passed. A fresh break on that close may start a new watch. |
| BUY / SELL CONFIRMED | A later close passes direction, displacement or momentum, and one of the two confirmation routes. Confirmation consumes the watch. |
| ENTRY VALID | The separate risk plan passes the order window, occupancy, extension and risk checks. A three-unit market order and bracket are submitted to the emulator. |
| ENTRY REJECTED | At least one risk or execution gate fails. Pine Logs retain the geometry and every failing reason. |
| NATIVE FILL / CLOSED | The emulator reports a fill or exit. If both occur within one candle, the CLOSED row carries both prices and timestamps. |

Direction uses the close relative to EMA21, the ordering of EMA21 and EMA55, and the sign of EMA21's change from three bars earlier. Displacement requires a directional body of at least 0.80 prior ATR14, at least 55% of the candle's range, and a close in the directional final quarter of that range. Momentum requires a directional close-to-close change over three bars of at least 0.80 prior ATR14. Either displacement or momentum can qualify.

Continuation requires a close beyond the frozen watch close in the same direction. The retest route uses a newly disclosed strict two-left/two-right lower high for SELL or higher low for BUY. The pivot must have formed on or after the watch bar, lie within 0.25 prior ATR14 of the broken level, and be followed by a confirmation close on the breakout side. Every comparison uses closed chart bars. The pivot becomes visible two bars after its formation.

The Data Window exposes prior-range break, displacement, momentum, EMA direction, newly disclosed pivots, rejection at the frozen level, volatility expansion, overextension and exhaustion. A rejection touches the frozen level and closes back on the breakout side. A range at or above 1.25 prior ATR14 marks volatility expansion. Exhaustion requires prior directional momentum, an excursion of at least 2.50 prior ATR14 from EMA21, a wick against that direction covering half the current range, and deceleration in the three-bar close change. The panel classifies the London and New York sessions by their local clocks. It displays news as `UNKNOWN` because this script has no timestamped news input.

## Plan and execution

For BUY, freeze the lesser of the broken level and the lowest price of the current and prior two bars, then subtract 0.10 prior ATR14 and round the stop down to the tick. For SELL, use the greater of the broken level and the highest price across the same bars, add the buffer and round up. Require positive initial risk between 0.25 and 2.50 prior ATR14, with the confirmed close at most 2.50 prior ATR14 from EMA21. The target is a 2R price projection from the confirmed close, rounded away from it. The calculation uses the planned close. A next-tick gap can change the actual fill, risk and reward.

The declaration uses USD 100,000 capital, one position, three fixed units, USD 0.175 per unit per fill, two ticks of slippage on applicable orders, 100% margin, standard OHLC fills, and calculation on bar close. The market entry and bracket are submitted together. A position still open after 24 bars, counting the entry bar, gets a next-tick time exit. The close cancels the bracket before submitting the time order. Native intrabar order sequencing follows TradingView's emulator. An OHLC research simulation can use conservative stop-first handling when both bracket levels are reachable in one candle. Track these paths separately when comparing results. [TradingView strategy order execution](https://www.tradingview.com/pine-script-docs/concepts/strategies/)

## TradingView research layout

Use the impulse and Premium V2 strategies as two chart entries and select `S5` in Premium V2. The impulse panel defaults to bottom right and Premium V2's S5 panel can sit top right. Each chart entry retains its own Strategy Report, positions, costs and fills. Use the study's portfolio replay to evaluate shared capacity explicitly.

The impulse strategy defaults to 1,200 calculated bars. Record the first processed bar and the completed cutoff, along with symbol, timeframe and Strategy Properties for comparisons. Enable **Emit decision and execution records to Pine Logs** and pick a cutoff aligned with a chart close. Leave the audit start before the first processed bar for a complete accounting report. The optional OHLC records allow the source bars to be checked against the two rolling fingerprints in the snapshot. In each event row, the leading timestamp records when the chart close made the information known. Export Pine Logs separately for each timeframe.

The [Pine source](../unified/experimental/QIRVANTA_SNIPER_V21_IMPULSE.pine) uses confirmed bars, prior-only range extrema and no plot offset or higher-timeframe data requests. A shortened history changes the warmup and initial watch state. Compare event timestamps across runs using the same first bar and Strategy Properties.

## Matched native decisions and execution

The TradingView exports share a cutoff of 2026-11-10 15:00 UTC. Each full OHLC tape reproduces its Pine snapshot's two rolling fingerprints. An independent replay on those bars matches the watch, confirmation and expiry timestamps, the frozen plan prices and risk gates, and the admitted IDs. Entry and exit times, prices, quantities, reasons, costs and net cash also match on these tapes.

| Frame | Confirmed bars | Watch, confirmation and expiry records | Plan decisions | Closed trades | Net USD |
|---|---:|---:|---:|---:|---:|
| M5 | 1,198 | 134 | 34 | 8 | −84.429 |
| M15 | 1,199 | 150 | 41 | 10 | −172.251 |
| M30 | 1,199 | 134 | 34 | 9 | −248.910 |

Five prefix replays per frame kept the same first bar and reproduced every earlier decision through their respective last close. This checks the recorded bar-close behavior against a causal reconstruction. Live tick behavior and a same-chart reload comparison are separate checks. The execution agreement applies to the observed intrabar paths on these tapes.

On the November 10 example, M5 observed a SELL watch at 09:05 UTC and confirmed it at 09:10. The plan failed the predeclared 2.50 ATR ceilings. Distance from EMA21 was 3.59 prior ATR and the structural stop required 4.03 prior ATR. The first M15 and M30 confirmations also failed both gates. The records distinguish directional information from an approved plan.

From the repository root, run the verifier against the native CSVs with the OHLC rows present.

```text
python -m unified.tools.verify_v21_causality --logs M5.csv M15.csv M30.csv --out verified.json
```

[Native comparison evidence](v21_evidence/impulse_native_causality.json) · [Verifier](../unified/tools/verify_v21_causality.py)
