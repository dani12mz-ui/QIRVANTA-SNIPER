# Phase 11 paper engine

Phase 11 extends the Phase 10 SQLite paper engine. Each registered symbol and strategy has its own database, initial virtual cash, reserved exposure, campaign history and report. All fills describe local simulation.

1. The foreground observes a broker quote and local receipt time. It supplies closed broker bars, the session state, current contract metadata and point-in-time calendar coverage. The first observation after startup or a data gap cannot start a campaign.
2. An eligible closed-bar plan is assessed on the current bid and ask by the independent guardian. Admission reserves exposure, records the signal ID and stable campaign ID, and waits for a later observation.
3. At an eligible later quote the engine reruns risk and news checks. Long market entry uses ask plus adverse slippage. Short entry uses bid less adverse slippage. A local limit requires an executable-side touch and uses the requested price. Expiration, missing data or changed risk can cause a missed entry.
4. The engine observes bid for longs and ask for shorts. A crossed stop precedes the holding limit and target processing. Stops and time exits use the observed executable side with adverse slippage. Targets use the registered target price once the observed executable side reaches it, which approximates limit eligibility without assuming queue position or price improvement.
5. The guardian allocates three executable quantities. TP1 enables a price break-even stop by default. An optional registered profit lock or ATR trail may tighten the stop for later observations. Fees and slippage can make a price break-even exit lose cash. Each position closure counts as one campaign regardless of component fills.

The journal preserves the structural stop, observed fill, current stop, targets, component quantities, fees, declared slippage, realized P&L, holding duration, net R and exit reason. Unrealized P&L marks the open remainder at the latest observed executable side with modeled exit costs. The report includes the mark timestamp and age. Financing is excluded pending verified instrument terms.

## Recovery and coverage

Each quote receipt, fill, state change and decision is atomic within its own journal. Repeating identical evidence has no effect. Conflicting content under the same quote ID is rejected. The roster can have committed only some wallets when a process is interrupted. Each committed ledger remains verifiable. Resubmitting the same eligible input can finish an interrupted batch while its quote is fresh. An expired quote cannot backfill a fill. The next discontinuity cancels pending entries and marks open paths uncertain. An open campaign can then close on the new observed quote. Formal forward acceptance excludes samples with uncertain campaign paths.

Paid cash, marked equity and closed campaign results have separate totals. An open position or pending entry at the end of the registered interval remains right-censored or unfilled. The report uses no imputed liquidation price. The SQLite files retain observations and decisions and grow over time. Keep each dated run as an independent roster and archive its complete directory after the interval.
