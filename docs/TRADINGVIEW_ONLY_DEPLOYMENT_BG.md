# TradingView Only · Стартиране

1. Отворете [експерименталния Pine файл](../unified/experimental/QIRVANTA_TRADINGVIEW_FINAL.pine) и копирайте пълния код в нова Strategy чернова в Pine Editor. Използвайте Add to chart. При Basic може да се наложи свободно място на графиката. Запазеният Champion има собствен файл. [Champion](../unified/QIRVANTA_UNIFIED.pine)
2. Изберете OANDA:XAUUSD, обикновени OHLC свещи и M5, M15 или M30. За базова проверка оставете `S0`, портфейл `RAW` и „Изследване на вход и изход“ OFF. S0 пази оригиналния модел, избор и разпределение.
3. За сравнение изберете само един режим. Фиксирайте периода, началния капитал, позиция 3 единици, комисиона 0,175 USD на единица за изпълнение и slippage 2 ticks. Запазете setup и Properties за всяко изпълнение. Не смесвайте резултати от различна видима история.
4. `STANDARD` показва статуса, Entry/SL/TP1–TP3 и H1/H4. `MINIMAL` е по-компактен. За push следвайте [нативните настройки за известия](TRADINGVIEW_ONLY_ALERTS.md).
5. За чисто сравнение първо сменете само S/F режима. При изследване на управление сменете един входен или изходен параметър. Оставете F1 в архивната група и S10 блокиран.

Локалните команди са за разработка и възпроизвеждане на доказателствата. Отворете PowerShell в корена на хранилището.

```powershell
python -X utf8 -B unified/tools/build_tradingview_final.py --check
python -X utf8 -B unified/tools/observe_tradingview_final.py ../native-tv-observer.txt --timeframe all
python -X utf8 -B -m unified.tools.audit_tradingview_only --output ../archive-recheck.json
python -X utf8 -B -m unified.tools.audit_tv_observations ../export.csv --output ../native-recheck.json
```

Наблюдателят пише отделен временен буфер за Pine Editor и премахва финалния модул за рисуване и `alert()` заради лимита на компилатора. Търговските извиквания остават в кода. Експортирайте Pine Logs при един режим и една рамка. За M5/M15 наблюдението е до 09.11.2026 02:45 UTC, за M30 до 02:30 UTC. Логовете са за сравнение на кампании и части. Използвайте пълния Pine файл за графиката и известията.

Изпълнението на потребителските функции е изцяло в TradingView. Съществуващите по-ранни части от репозиториума са отделна история.
