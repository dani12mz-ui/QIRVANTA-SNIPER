# Локално стартиране на QIRVANTA Ultimate

Използвай Python 3.12 и изпълнявай командите от корена на checkout. Наблюдаваният runtime е CPython 3.12.14, Windows AMD64 и `tzdata==2026.6`. В отделна виртуална среда инсталирай само пакета от hash lock. При сравнение на session часове използвай еднаква IANA база. [Lock](../qirvanta_ultimate/requirements.lock)

```powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install --require-hashes -r qirvanta_ultimate/requirements.lock
$env:PYTHONTZPATH = ''
.venv/Scripts/python.exe -X utf8 -B -m qirvanta_ultimate fingerprint
.venv/Scripts/python.exe -X utf8 -B -m unittest discover -s tests -p test_ultimate_service.py -v
```

Архивният read-only анализ използва проверения SHA256 и последния M15 cutoff. Разходите, изпълненията и P&L от champion са в отделния native audit.

```powershell
python -X utf8 -B -m qirvanta_ultimate review --bars diagnostics/protocol/datasets/B-ALL-FULL/bars.csv --sha256 7015f6386d2627237c57ff8625d59cbe3c9b852e4742b786cbb4679db3d48c56 --symbol OANDA:XAUUSD --source OANDA --at-ms 1794192300000 --family S3 --family S5 --family S8
```

За paper подготви локална директория извън checkout. `profile.json` съдържа изричните параметри на `PaperProfile` в `qirvanta_ultimate/service.py`. Идентичността е `source`, `symbol`, `quote_server` и `account_currency`. Количествата използват `contract_size`, `point`, `tick_size`, `loss_per_tick_lot`, `profit_per_tick_lot`, `min_lot`, `max_lot` и `lot_step`. Рискът изисква `margin_per_lot`, `stops_level_points`, `fee_per_lot_per_fill`, `max_spread`, `max_entry_deviation` и `risk_fraction`. `calendar_source` определя точния одобрен HTTPS източник. Режимът е `PAPER`, по подразбиране `MARKET_NEXT_QUOTE`, а `LIMIT` се избира преди регистрация.

`draft.json` задава `protocol_id`, `timeframe_ms`, `families`, `history_cutoff_ms`, `start_ms`, `end_ms`, `initial_cash` и по избор оценъчните прагове на `PaperProtocol`. Всички времена са целочислен Unix UTC ms. Използвай бъдещ прозорец и отделен journal за всеки проспективен експеримент. Този guide не присвоява broker характеристики на OANDA или на тестовите числа.

```powershell
python -X utf8 -B -m qirvanta_ultimate register --db ../local/forward.sqlite --profile ../local/profile.json --draft ../local/draft.json
python -X utf8 -B -m qirvanta_ultimate serve --db ../local/forward.sqlite --profile ../local/profile.json --spool ../local/spool --bars-root ../local/bars --calendar-root ../local/calendar --once
python -X utf8 -B -m qirvanta_ultimate health --db ../local/forward.sqlite
python -X utf8 -B -m qirvanta_ultimate notifications --db ../local/forward.sqlite
python -X utf8 -B -m qirvanta_ultimate report --db ../local/forward.sqlite
```

Производителят записва котировките атомарно в нови файлове. Допустимият JSON съдържа `source`, `quote` с `symbol,bid,ask,at_ms,server`, булево `session_open`, `bars_sha256` и незадължителен `calendar_sha256`. Баровете се съхраняват в `bars/<sha256>.csv` с колони `time,time_close,open,high,low,close`. Snapshot на календарът е `calendar/<sha256>.json` със схемата от [примерното BLS наблюдение](ultimate_evidence/bls_schedule_observed_20261109.json). BLS наблюдението демонстрира формат и ограничен обхват. Paper проверката изисква одобрен пълен календар. Пакетите трябва да пристигат актуални и с растящ UTC timestamp. `serve` коригира реда само в текущата партида и отхвърля закъснял пакет след по-нова вече обработена котировка.

Стартиране без `--once` следи локалната директория до края на протокола или Ctrl+C. Ръчната пауза се записва с `pause --db ../local/forward.sqlite on`. За възстановяване стартирай същия checkout, profile и journal. Преди обновяване спри процеса, направи SQLite backup чрез `sqlite3.Connection.backup` и запази journal head. За rollback използвай същия стар source fingerprint и неговата консистентна база. Drift изисква нов протокол и нов journal.

Изпълнената startup проверка използва тестова конфигурация и празен spool. Настоящият release не е инсталиран като Windows услуга и не събира нови данни във фонов режим. [Доказателство за локално стартиране](ultimate_evidence/service_runtime_evidence.json)
