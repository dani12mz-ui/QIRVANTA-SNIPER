# Phase 11 market data pipeline

## Forward quotes and sampled candles

The foreground runtime pulls the latest `symbol_info_tick` snapshot for each registered exact symbol. The requested polling cadence is a sleep interval after the work of each pass. The synchronous pull iterator has no producer queue. A slow SDK call or analysis pass can delay the next observation. Vendor IPC initialization has a timeout. An in-process SDK data call has no separate hard deadline. A connection-level failure marks every registered symbol unavailable and retries with bounded backoff. A per-symbol read failure marks only that symbol unavailable. Foreground shutdown releases the SDK attachment and closes the SQLite writer.

Each observed update records a durable increasing sequence and a UTC receipt timestamp. Bid, ask, tick size and broker timestamp must be valid. The default maximum quote age and maximum between-sample gap are five seconds. The system distinguishes identical repeated snapshots from conflicting same-timestamp prices, out-of-order observations, future timestamps and stale prices. The first accepted sample after startup, reconnect, an outage, a detected gap or a changed price grid is a discontinuity. It cannot establish a price path through the outage. A read-only receipt lookup can only use an eligible earlier forward quote known by the receipt timestamp.

| State | Meaning |
| --- | --- |
| `LIVE_INCOMPLETE` | The UTC bucket is still active and contains observed BID samples. |
| `CLOSED_CONFIRMED` | A later accepted quote crossed the expected UTC boundary without a continuity failure. The OHLC summarizes observed samples. Availability starts at the actual confirmation receipt. |
| `MISSING_DATA` | A boundary, gap, restart, closed session or data failure prevented complete observation. Skipped buckets stay absent. |
| `STALE_SOURCE` | Freshness failed. The health view suppresses the previous quote as current pricing. |
| `REVISED_HISTORY` | A separately retrieved native historical snapshot changed against a supplied earlier manifest for the exact same scope. Both snapshots remain available. |

All seven sampling periods use fixed UTC epoch boundaries. Session windows are exact UTC half-open intervals verified separately for each symbol with explicit coverage dates. Both the vendor quote time and local receipt must fall in an open interval for the session gate. Incomplete boundaries are invalidated on restart. `feature_bars()` can use earlier confirmed sampled candles across gaps once the latest UTC boundary has a new confirmed close after the last discontinuity. Time gaps remain visible. The runtime requires live confirmation for prospective decisions and can use a separately registered BID history manifest for earlier warmup only.

The sampler can miss intra-poll extremes. An illiquid feed, brief disconnect, scheduled pause or timezone-dependent venue close may interrupt sampled bars even when the broker can later supply native history. A missing candle yields no fabricated price or historical execution. The existing entry analysis validates M5, M15 and M30. The candle store supports all seven periods for observation. Broker trade volume is unavailable from the latest quote samples.

The local `market.sqlite3` tracks feed freshness, event counts, current bucket coverage and latest confirmed close for each symbol and period. State and audit updates share an SQLite transaction. Event counts measure poll outcomes and provide no direct elapsed-time uptime estimate. Storage grows with observations and confirmed candles. Keep the state under a private runtime root, check free space and use an appropriate local retention procedure.

## Historical native cache

With an authorized running connection and validated private config, capture a UTC half-open window using an exact configured symbol.

```powershell
python -B -m qirvanta_ultimate.phase11 cache-bars --root <private-root> --config <local-config.json> --symbol <exact-symbol> --minutes 15 --start-ms <utc-ms> --end-ms <utc-ms>
```

Each retrieval writes `native-history/<manifest-sha256>/manifest.json` and `bars.json` under the selected runtime root. The manifest records exact symbol, provider, server, period, UTC request bounds and coverage gaps, the actual retrieval timestamp, row count, dataset hash, chart mode and reported volume semantics. Gaps wholly covered by a reviewed closed-session window are marked separately. Other gaps stay unclassified. The reader preserves native bar opens. Close estimates from nominal timeframe duration support coverage reporting and cannot establish actual venue-defined D1 boundaries. Warmup access is limited to UTC-aligned M5, M15 or M30 and the runtime requires BID chart mode.

Pass `--prior-manifest` when deliberately checking a repeated capture of the exact same window. A changed dataset becomes a separate `REVISED_HISTORY` artifact. The cache validates the manifest and row hashes on read. A retrieval taken after a market event becomes available for feature warmup at or after that retrieval, and cannot supply fills or prospective historical outcomes. Native data stays separate from the OANDA/TradingView archives. Publish only permitted metadata and sanitized evidence.

Synthetic regression tests exercised all seven UTC periods, outage and restart handling, receipt-time causality, delayed and out-of-order data, session gates, two-symbol isolation, backoff and immutable revisions. The actual environment did not supply live MT5 quotes, verified trading sessions or native broker history. The tests establish local pipeline behavior without measuring market performance.
