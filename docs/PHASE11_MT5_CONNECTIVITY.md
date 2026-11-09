# Phase 11 MT5 connectivity

## Read-only boundary

The reader attaches to an explicitly selected running terminal executable and binds the observed account mode, server and broker company. The connector rechecks terminal connection and account identity around SDK reads and rejects an observed identity change. It reports account kind, currency and observed terminal and account trading permission flags. The read-only mode applies even when the terminal reports that the account itself can trade. The operator must keep the selected terminal running during attachment because the vendor SDK can start a terminal if its process exits between the local check and SDK initialization. [MetaQuotes initialize documentation](https://www.mql5.com/en/docs/python_metatrader5/mt5initialize_py)

`MT5Reader.symbol()` inspects exact server symbols, watchlist visibility and selection, chart and execution modes, filling modes, point and tick sizes, tick values, contract size, volume bounds and step, stop and freeze distances, and currency fields. Metadata and bid/ask observations are kept separately. `market_tick()` reads the latest vendor snapshot with UTC milliseconds and maps an absent last-trade price to null. `recent_ticks()` retrieves at most 10,000 historical observations from a bounded start window and labels them `BACKFILLED`. It preserves tied vendor timestamps. `bars()` returns native M1, M5, M15, M30, H1, H4 and D1 bars with provider open timestamps and reported volumes. [MetaQuotes Python integration](https://www.mql5.com/en/docs/python_metatrader5), [Current quote](https://www.mql5.com/en/docs/python_metatrader5/mt5symbolinfotick_py), [Historical ticks](https://www.mql5.com/en/docs/python_metatrader5/mt5copyticksfrom_py), [Native bars](https://www.mql5.com/en/docs/python_metatrader5/mt5copyratesrange_py)

The official Python SDK lists no direct trading-session query equivalent to the MQL5 `SymbolInfoSessionTrade` function. Phase 11 accepts separately reviewed UTC session windows with a bounded validity horizon. During an uncovered interval the session remains unknown. Account numbers, passwords and local terminal paths are omitted from snapshots and logged errors. [Official Python API index](https://www.mql5.com/en/docs/python_metatrader5), [MQL5 SymbolInfoSessionTrade](https://www.mql5.com/en/docs/marketinformation/symbolinfosessiontrade)

## Local Windows setup

From the repository, run `python -B -m qirvanta_ultimate.mt5 probe` with the interpreter intended for the runtime. For the next verification, use the official MetaTrader5 package in an authorized Windows Python environment and an existing, connected terminal. Set the following environment variables in the local session, using values verified on that terminal.

| Variable | Local value |
| --- | --- |
| `QIRVANTA_MT5_TERMINAL_PATH` | Absolute path of the chosen running terminal executable |
| `QIRVANTA_MT5_EXPECTED_SERVER` | Exact observed broker server string |
| `QIRVANTA_MT5_EXPECTED_COMPANY` | Exact observed broker company string |

For a verified demo read-only binding, inspect exact matches and then inspect the chosen symbol. For an explicitly authorized live-account observation, use `LIVE_READ_ONLY` in place of `DEMO_READ_ONLY`.

```powershell
python -B -m qirvanta_ultimate.mt5 inspect --mode DEMO_READ_ONLY --contains XAU
python -B -m qirvanta_ultimate.mt5 inspect --mode DEMO_READ_ONLY --contains BTC
python -B -m qirvanta_ultimate.mt5 inspect --mode DEMO_READ_ONLY --symbol XAUUSD.s
```

Run the last command only if discovery returns `XAUUSD.s`. Use the exact returned BTC name with its own metadata. The operator's terminal must already show the selected symbol. Keep inspection and price outputs private.

The observed Windows probe found no compatible running terminal and no discoverable SDK, so the actual connector did not initialize. Local tests use explicit synthetic SDK fixtures. They verify identity checks, UTC parameters, quote and history separation, metadata and reader shutdown. Actual PU Prime connectivity, permission flags, symbol metadata, venue hours, market prices and broker history remain unverified. See [the environment discovery](PHASE11_ENVIRONMENT_DISCOVERY.md).
