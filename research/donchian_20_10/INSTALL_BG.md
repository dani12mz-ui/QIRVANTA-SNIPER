# Инсталиране и възпроизвеждане

1. В разрешения тестов TradingView профил отвори `OANDA:XAUUSD` с обикновени свещи. Използвай D1 за първичния тест и 4H за вторичния.
2. В нов **Untitled script** в Pine Editor постави целия [Pine v6 файл](QIRVANTA_DONCHIAN_20_10_RESEARCH.pine) и избери **Add to chart**. За проверката е достатъчна незапазена инстанция. Ако друг скрипт използва същия ъгъл, можеш временно да го скриеш в тестовия изглед.
3. В Strategy Report използвай стандартния режим. Зададените начални свойства са 100 000 USD, 100% margin, 0,175 USD на единица на страна, 2 тика, Bar Magnifier изключен. В менюто на стратегията избери **Pine logs…** и **Download logs**. Журналът съдържа сигнали, изпълнения, изходи и метаданни.
4. Потвърденият BUY/SELL е върху свещта на сигнала. Нативната стрелка е симулираното изпълнение. `SIM LONG` и `SIM SHORT` показват състояние на емулатора. След историческата отсечка двигателят не допуска нови входове.

Нужни са Python 3.11 или по-нов и IANA timezone данни за `America/New_York`. На Windows инсталирай `tzdata`, ако стандартният `zoneinfo` не открива зоната. Следващите команди се изпълняват от корена на хранилището. За `--local-m30` използвай по-ранния локален OANDA архив с SHA-256 от [манифеста](evidence/data_manifest.json). Суровият архив има колони `time,time_close,open,high,low,close` и не се публикува в клона. Използваният локален път е `../mission/M30_dataset/bars.csv`.

```powershell
python -B -m unittest discover -s tests -q
python -B -m unittest research.donchian_20_10.test_controls -q
python -B -m research.donchian_20_10.data --local-m30 ../mission/M30_dataset/bars.csv --private-dir ../donchian-private --manifest ../donchian-private-manifest.json
python -B -m research.donchian_20_10.evaluate --private-dir ../donchian-private --native research/donchian_20_10/evidence/native-logs.zip --manifest ../donchian-private-manifest.json --output ../donchian-results
python -B -m research.donchian_20_10.controls --common-history-proven --output ../donchian-controls.json
python -B -m research.donchian_20_10.native_runtime research/donchian_20_10/evidence/native-logs.zip --output ../donchian-native-runtime.json
python -B -m research.donchian_20_10.fine_audit --local-m30 ../mission/M30_dataset/bars.csv --reconstructed ../donchian-private --output ../donchian-finer-audit.json
```

За възпроизвеждане на записаните нативни QA копия използвай `python -B -m research.donchian_20_10.make_native_probe 223 ../D1-qa.pine` и `python -B -m research.donchian_20_10.make_native_probe 1336 ../H4-qa.pine`. Тези броеве са за наблюдавания последен бар. При друга крайна свещ промени броя само за временния одит, докато `D20_META.first_time` е 01.01.2026 22:00 UTC. Сигналните правила и размерът на позицията остават същите.
