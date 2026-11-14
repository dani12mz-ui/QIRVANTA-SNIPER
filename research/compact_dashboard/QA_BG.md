# QIRVANTA GOLD PRO · COMPACT

Пълният Pine v6 кандидат е компилиран и проверен в TradingView. Основният изглед показва четири реда: символ и рамка, режим и час UTC, разрешение и посочно наблюдение. При наличен план добавя Entry, SL и един ред с TP1/TP2/TP3 в този ред. При липса на план ценовите клетки се изчистват. При остарял последен бар се показва **LAST CLOSE · STALE**.

COMPACT и MOBILE ползват една таблица. Настройката **ТАБЛО → Покажи таблото** управлява видимостта за конкретния екземпляр. **Pressure в COMPACT** е изключено по подразбиране. При включване показва същите проценти и вида на проксито. За подробностите избери **FULL AUDIT**. За компактна снимка на историческо затваряне включи историческия преглед във FULL AUDIT и **COMPACT при исторически преглед**. Съществуващият превключвател за исторически преглед и самият час на избрания бар остават източник на състоянието.

Използвани са оригиналните статус, посочен източник, референтни цени и проверка за показване на план. При M30 11.11.2026 12:00 UTC се вижда **CORE HTF DATA MISSING** за липсващия основен D1 контекст и отделно **SELL WATCH**. При M15 12:30 се виждат **WAIT** и **SELL BREAKOUT CONFIRMED**. Само диагностичният принцип на показване на готовността е взет от изследването `56c9dd2`. Няма допълнителен HTF request, промяна на `calc_bars_count`, на условията за READY, на Pressure, на цените на плана или на графичните зони. [Източников паритет](evidence/source-parity.json)

| Нативна сверка, 11.11.2026 00:00–17:30 UTC | M5 | M15 | M30 |
| --- | ---: | ---: | ---: |
| Затваряния | 211 | 71 | 36 |
| CORE / READY / RAW събития | 0 / 0 / 0 | 20 / 2 / 6 | 0 / 0 / 0 |
| Luminous събития | 66 | 13 | 7 |

Двете версии работиха на една OANDA:XAUUSD графика със стандартни свещи. Нативният компаратор потвърди еднакъв 1223-баров OHLC префикс, край 13.11.2026 22:00 UTC и пълен паритет на сравняваните payload-и, Pressure, Shadow, Luminous и RAW PAPER. M15 упражнява един симулиран вход, едно затваряне и един отказ при отваряне. M5 и M30 не упражняват изпълнения в този прозорец. Отделна двойка журнали за историческо затваряне на M15 в 12:30 UTC потвърди WAIT, потвърдено SELL наблюдение и липса на видим план и в двете версии. Проверени са alert payload-ите в Pine Logs. Доставка не е тествана. Успешни са 189 локални теста. [Настройки](evidence/native-settings.json) · [Паритет](evidence/native-parity.json) · [Оригинални журнали](evidence/native-logs.zip)

| Реални кадри | Visual V2 | COMPACT |
| --- | --- | --- |
| M15 12:15 BUY план, тъмно | [Преди](evidence/M15-buy-dark-before.jpg) | [След](evidence/M15-buy-dark-after.jpg) |
| M15 02:15 SELL план, тъмно | [Преди](evidence/M15-sell-dark-before.jpg) | [След](evidence/M15-sell-dark-after.jpg) |
| M15 12:30 WAIT и потвърден SELL, без план | [Преди](evidence/M15-wait-dark-before.jpg) | [След](evidence/M15-wait-dark-after.jpg) |
| M30 12:00 исторически, липсващ CORE D1 | [Преди](evidence/M30-history-core-missing-dark-before.jpg) | [След](evidence/M30-history-core-missing-dark-after.jpg) |
| M15 12:15 BUY план, светло | [Преди](evidence/M15-buy-light-before.jpg) | [След](evidence/M15-buy-light-after.jpg) |
| M15 12:15 BUY план, 390 × 844, светло | [Преди](evidence/M15-buy-390-light-before.jpg) | [След](evidence/M15-buy-390-light-after.jpg) |

Двойките използват същата графика, zoom и ценова скала, без ръчно свиване на ценовия диапазон. Мобилният исторически BUY кадър използва позиция **Bottom right**, за да остави легендата на TradingView свободна. Отделно е заснет [MOBILE с автоматична позиция, 390 × 844](evidence/M30-mobile-390-dark.jpg). В първия тест мобилният изглед изряза отделните TP клетки. Финалният билд показва пълните цени в един ред и е проверен повторно. Пазарът е затворен по време на теста. Плановете са документирани при избрано историческо затваряне, а текущият кадър упражнява остарял последен бар. [Компилация](evidence/compile-M15.jpg) · [Pressure ON](evidence/M30-compact-pressure.jpg) · [FULL AUDIT](evidence/M30-full-audit.jpg)

Възпроизвеждане от корена на репозиторията:

```powershell
$qa = 'research/compact_dashboard'
$env:PYTHONPATH = (Get-Location).Path
python -B -m zipfile -e "$qa/evidence/native-logs.zip" "$env:TEMP/qirvanta-compact"
python -B "$qa/compare_native.py" "$env:TEMP/qirvanta-compact/native" --candidate "$qa/QIRVANTA_GOLD_PRO_COMPACT.pine" --settings "$qa/evidence/native-settings.json" --review-case "15@2026-11-11T12:30:00Z=$env:TEMP/qirvanta-compact/native/wait" --output "$env:TEMP/qirvanta-compact/parity.json"
python -B -m unittest discover -s tests
```
