# Phase 11 environment discovery

The available Windows runner was inspected on 2026-11-09 around 19:35 UTC. The probe read process and installation metadata without attaching to a terminal or reading account files. It describes the accessible runner and its visible profile.

| Check | Observed result |
| --- | --- |
| Operating system | Windows 26H2 build 26300.9818, AMD64 |
| Python used in tests | Bundled CPython 3.12.14, 64-bit, SQLite 3.53.1 |
| Other interpreter and environments | A `python.exe` command was discoverable, the `py` launcher was unavailable, and no virtual environment or Conda environment was active. No top-level `.venv`, `venv` or `env` directory appeared in the checkout. |
| Installation indicators | Zero matching MetaTrader, MetaQuotes, PU Prime or QIRVANTA uninstall entries in the inspected registry keys. Zero matching MetaTrader or MetaQuotes directories at the inspected common installation roots. No MetaQuotes directory at the inspected Local or Roaming profile locations. Custom paths remain unverified. |
| Running terminal | Zero matches among `terminal64`, `terminal`, `metatrader64` and `metatrader`. The MT5 probe observed zero accessible running executable paths. |
| Authorized terminal configuration | No visible environment variables beginning with `QIRVANTA_`, `MT5_` or `TELEGRAM_`. Terminal path, company and server binding were unavailable. |
| SDK | `find_spec('MetaTrader5')` returned unavailable for the tested interpreter. |
| QIRVANTA | Source checkout available. No matching installed application or named PU Prime/QIRVANTA data directories in the inspected profile roots. |
| Paper journal | No visible `.sqlite`, `.sqlite3`, `.db` or `.duckdb` candidate in the checkout before Phase 11 test fixtures. Existing journal paths outside the inspected scope are unknown. |
| Market data | The repository has earlier OANDA/TradingView research archives and synthetic fixtures. Broker-native data, a PU Prime account, and exact gold or bitcoin symbol metadata were unavailable. |
| Telegram | No authorized connection was demonstrated by the environment probe. |
| Services and scheduled tasks | Service and scheduled task enumeration were denied in this runner. Existing installations in those scopes remain unknown. |
| Repository | Phase 11 branch `engineering/qirvanta-phase11-live-paper` started at `f9bcf7430ec0c50d013da93c155fb79f5684bade`. |
| Disk and logs | 388.16 GiB free on the working drive at the final probe. Runtime databases and native history belong under the operator's private `--root` outside the checkout. The repository's existing evidence logs are separate. |

Actual read-only probe command and stdout:

```powershell
python -B -m qirvanta_ultimate.mt5 probe
```

```json
{"attachment_attempted": false, "broker_orders_enabled": false, "running_terminal_path_count": 0, "sdk_discoverable": false, "windows": true}
```

The absent SDK and running terminal blocked attachment. Terminal identity and permissions, PU Prime account mode and currency, exact symbols, contract and session metadata, current prices, recent ticks and native historical bars were unverified in this environment. No live quote, paper fill or Telegram delivery can be inferred from the local regression tests.
