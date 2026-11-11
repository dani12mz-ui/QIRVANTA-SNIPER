# GOLD PRO market context

Both generated Pine scripts use the same canonical market-data source on standard OANDA:XAUUSD M5, M15 and M30 charts. The first 200 loaded chart bars supply the rolling OHLC fingerprints. Each H1, H4 and D1 snapshot exposes the source candle's opening time and confirmed close time. [Shared Pine source](../unified/experimental/gold_pro/market_data.pineinc)

V2.2 used a recursively seeded EMA inside each higher-timeframe request. The indicator had a 3,600-chart-bar calculation limit while the strategy used uncapped history. During the November 10 case, their H4 values led to different context at the M5 13:25 close. GOLD PRO uses a fixed 110 completed-source-bar window in each request. EMA21 and EMA55 start with the oldest close and update chronologically through the latest completed source close. Both scripts use the same uncapped chart declaration and the same finite calculation. Matching source closes produce matching EMA values regardless of older history. A new higher-timeframe candle first discloses the previous candle to a chart bar whose opening timestamp is at or after that source close. [V2.2 case](V22_NOVEMBER_10_CASE.md) · [TradingView higher-timeframe requests](https://www.tradingview.com/pine-script-docs/concepts/other-timeframes-and-data/)

H1/H4 require 110 available source candles. At a confirmed chart close, their source close must already have been available at chart open. The H1 age limit is two hours and the H4 limit is eight hours. D1 uses the same EMA window and a 96-hour age limit for advisory context. A missing, stale or under-initialized source yields UNKNOWN. NEUTRAL means the values are available and fail both strict bullish and bearish EMA ordering. H1 or H4 UNKNOWN blocks entry eligibility. D1 is advisory. These cutoffs are declared research assumptions and apply even during scheduled market gaps.

The price and timestamps of the latest 200 chart bars produce two modular fingerprints. Each prices-in-ticks token also includes time, and the second fingerprint weights the OHLC by chart-open seconds modulo 65,521. The same 200 consecutive observed bars produce the same pair independently of earlier loading. The fingerprints help detect changes in these fields and are supplemented by SHA-256 of exported files. Fingerprints do not include volume or higher-timeframe prices. The paired native audit compares every logged context field including higher-timeframe timestamps, prices and directions.

Asia uses 09:00–18:00 Tokyo time. London and New York use 08:00–17:00 in their own IANA time zones on weekdays. When more than one block is active, the dashboard shows London–New York overlap, then New York, London and Asia in that order. The exchange provides the actual candles and the native trading-day timestamp. Each observed range records the first available bar and whether coverage is continuous from the block's start. Weekend and daily closures do not create invented bars. [TradingView IANA time zones](https://www.tradingview.com/pine-script-docs/concepts/time/)

The Python archive runner infers H1 from UTC hours, H4 from the observed OANDA New York anchors and D1 from the observed 18:00-to-17:00 exchange cycle. It requires complete chart coverage inside an inferred bucket, uses UTC timestamps and preserves gap boundaries. An incomplete bucket clears its reconstructed warmup. The native scripts use provider higher-timeframe candles directly. The derived archive feed can produce UNKNOWN when a native provider candle exists, and the count is visible in the archived audit. D1 bucket provenance is inferred in Python and is advisory. [Python context](../unified/tools/gold_market_data.py)

| Frame | Pinned chart bars | Eligible H1/H4 | H1 source comparisons | H4 source comparisons |
|---|---:|---:|---:|---:|
| M5 | 5,565 | 5,385 | 2,997 | 2,805 |
| M15 | 6,445 | 4,617 | 3,399 | 3,235 |
| M30 | 10,091 | 5,996 | 3,496 | 3,332 |

The archive replay compared 9,918 chart OHLC rows with actual V2.2 exports, with exact timestamp and price equality. Every comparable H1/H4 source close and timestamp matched. It also preserved six chronological prefixes exactly. The V2.2 exports test archived source candles. GOLD PRO's EMA outputs and matching strategy/indicator context have a separate native audit. [Archive evidence](gold_context_archive.json)

```powershell
python -X utf8 -B -m unittest unified.tests.test_gold_market_data -v
python -X utf8 -B -m unified.tools.audit_gold_context_archive --data-root ../mission --logs-dir ../../outputs/v22 --out context-archive-check.json
```

To verify native parity, turn on **Log confirmed market context** on both drafts. Set an identical fixed UTC start and cutoff. Use the same symbol, frame and standard candles. Download Pine Logs CSV exports and pass each pair in the same frame order.

```powershell
python -X utf8 -B -m unified.tools.audit_gold_context --strategy strategy_M5.csv strategy_M15.csv strategy_M30.csv --indicator indicator_M5.csv indicator_M15.csv indicator_M30.csv --out context-native-check.json
```

Each report gives the covered frames, exact time range and native export hashes. It validates timestamp order, UNKNOWN handling, session ranges, two OHLC fingerprints and equality across the two scripts. The independent finite EMA recomputation counts start at 110 distinct logged source closes. A shorter trace still tests equality, timestamps and context gating while reporting the available recomputations.
