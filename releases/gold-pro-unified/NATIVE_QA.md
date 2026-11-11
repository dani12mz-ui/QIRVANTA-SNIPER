# Нативен приемателен тест на QIRVANTA GOLD PRO UNIFIED

**Статус `PARTIALLY_COMPLETE`.** Пълният Pine v6 индикатор се изпълни на стандартни свещи `OANDA:XAUUSD` на M5, M15 и M30 на 11.11.2026 UTC. Паритетът за проверения прозорец е потвърден. Доставката на техническо известие не е проверена; при 390 px FULL AUDIT изисква компактна настройка. [Проверяван Pine файл](QIRVANTA_GOLD_PRO_UNIFIED.pine)

## Компилация и установен дефект

На M30 оригиналният файл рисуваше `QIRVANTA GOLD PRO RC2` в заглавката. Поправката заменя един текстов литерал в таблицата с `QIRVANTA GOLD PRO UNIFIED`. Пълният коригиран файл е добавен в TradingView и панелът е изобразен на трите интервала. SHA-256 `03150c89a587cc81559d8712118f8c0179eaf66496e61e0ec8689dba79c2c588`. [Преди поправката](evidence/screenshots/M30-before-title-fix.jpg) · [След поправката на M30](evidence/screenshots/M30-live-light.jpg) · [M15](evidence/screenshots/M15-live-dark.jpg) · [M5](evidence/screenshots/M5-live-desktop-dark.jpg) · [Pine v6: Compiled / Added to chart](evidence/screenshots/M5-native-pine-compile.jpg)

Сверката с `203f9d8a88a8c4b51a5dc646fc0f1752a596ba1f` допуска само четирите уводни коментара, името и краткото име на индикатора, включения по подразбиране Direct Trend Shadow и коригираната заглавка. Битовата идентичност на RC2 Audit и запечатания GOLD PRO forward протокол е потвърдена. SHA-256 на forward печата `09181ecaf9e30cde62c5203b524cb458d808a07c24029e961e32bb25c474afd1`. [Проверка на източниците и хешовете](evidence/parity.json)

## Проверки на графиката

| Проверка | Наблюдение в TradingView | Доказателство |
| --- | --- | --- |
| LIVE PREMIUM на desktop | M5 с тъмно платно показва `BUY DETECTED · RESEARCH / WAIT`. M15 с тъмно платно и M30 със светло платно показват `SELL WATCH` и отделно `WAIT`, време на потвърждение, възраст и `NO ACTIVE PLAN`. | [M5 тъмно](evidence/screenshots/M5-live-desktop-dark.jpg) · [M15 тъмно](evidence/screenshots/M15-live-dark.jpg) · [M30 светло](evidence/screenshots/M30-live-light.jpg) |
| Direct Trend Shadow по подразбиране | Действителният release файл отваря `Покажи наблюдение` включено. | [Inputs](evidence/screenshots/default-shadow-input.jpg) |
| BUY наблюдение | M15, 00:15 UTC. `BUY BREAKOUT CONFIRMED` и `BUY DETECTED · RESEARCH` при `WAIT`. Историческият панел няма план. | [00:15 UTC](evidence/screenshots/M15-0015-buy-detected-dark.jpg) |
| SELL WATCH | M15, 11:45 UTC. Shadow наблюдението е на възраст 0 минути при затваряне, а S5 разрешението е `WAIT`. | [11:45 UTC](evidence/screenshots/M15-1145-sell-watch-light.jpg) |
| Конфликт | M15, 12:00 и 13:30 UTC. Потвърденият Shadow SELL и отделният S5 `BUY WATCH` се виждат едновременно с източниците им. Разрешението остава `WAIT`. | [12:00 UTC](evidence/screenshots/M15-1200-conflict-dark.jpg) · [13:30 UTC](evidence/screenshots/M15-1330-conflict-dark.jpg) |
| S5 SELL READY | M15, 02:15 UTC. S5 разрешава SELL независимо от Shadow BUY наблюдението. `Entry ref. 4042.005`, `Initial SL 4053.568`, `TP1 4030.442`, `TP2 4018.879`, `TP3 4007.316`. Трите цели са R проекции. | [02:15 UTC](evidence/screenshots/M15-0215-sell-plan-light.jpg) |
| S5 BUY READY | M15, 12:15 UTC. S5 разрешава BUY независимо от Shadow SELL наблюдението. Таблото показва `Entry ref. 4046.645`, `Initial SL 4042.407`, `TP1 4050.883`, `TP2 4055.121`, `TP3 4059.359` и `R PROJECTIONS`. | [12:15 UTC](evidence/screenshots/M15-1215-buy-plan-dark.jpg) |
| SELL DETECTED и WAIT | M15, 12:30 UTC. `SELL BREAKOUT CONFIRMED`, `SELL DETECTED · RESEARCH`, потвърден ретест и `WAIT` без исторически план. Състоянието се вижда на тъмно и светло платно. | [Тъмно](evidence/screenshots/M15-1230-sell-detected-wait-dark.jpg) · [Светло](evidence/screenshots/M15-1230-sell-detected-wait-light.jpg) |
| Свежест на наблюдението | M15, 14:45 UTC. По-ранното SELL наблюдение вече не се показва. Панелът изписва `NONE`, `WAIT`, `NO HISTORICAL PLAN` и времето на прегледа. | [14:45 UTC](evidence/screenshots/M15-1445-expiry-dark.jpg) |
| Pressure | Видими пропорционални ленти, проценти и означения `PROXY` и `TICK VOL`. При 12:30 UTC OHLC показва 43.5% BUY и 56.5% SELL, а избраният tick-volume вариант показва 44.1% BUY и 55.9% SELL. | [OHLC](evidence/screenshots/M15-1230-sell-detected-wait-dark.jpg) · [Tick volume](evidence/screenshots/M15-full-audit-tick-light.jpg) |
| Браузър при 390 px | M5 `MOBILE` остава четим на двете платна. При M15 `FULL AUDIT` с `Standard` част от панела излиза извън платното. `Small` и ръчно `Bottom right` побират панела. | [MOBILE тъмно](evidence/screenshots/M5-mobile-390-dark.jpg) · [MOBILE светло](evidence/screenshots/M5-mobile-390-light.jpg) · [FULL AUDIT Standard](evidence/screenshots/M15-full-audit-390-light.jpg) · [FULL AUDIT Small, Bottom right, светло](evidence/screenshots/M15-full-audit-small-bottom-390-light.jpg) · [Тъмно](evidence/screenshots/M15-full-audit-small-bottom-390-dark.jpg) |

## Исторически паритет

Повторната обработка на запазените нативни журнали сравнява `203f9d8` Directional Visibility с тогавашния му RC2/S5 контрол при еднакви OHLC и warmup за всяка двойка. Краят е 11.11.2026, 17:30 UTC. RAW PAPER е симулираният регистър на индикатора и сумите в последната колона са в R. [Машинна сверка](evidence/parity.json) · [Архивни TradingView CSV](../../research/direct_trend_visibility/evidence/native-logs.zip)

| Интервал | CORE | READY | S5 съобщения | RC2 съобщения | RAW PAPER събития | DT Shadow събития | RAW PAPER нетно R |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| M5 | 42 | 6 | 44 | 38 | 32 | 532 | −2.00237566 |
| M15 | 20 | 2 | 19 | 17 | 6 | 499 | −0.04255369 |
| M30 | 0 | 0 | 0 | 0 | 0 | 493 | 0 |

Проверените архивни записи съвпадат по идентификатор, време, посока и планови цени, както и по RAW PAPER изпълненията и регистъра. M30 съдържа OHLC и Shadow събития, но няма S5 примери в тази извадка. Архивният M15 журнал също включва `SELL READY` с валиден план при 02:15 UTC. [Архивен нативен отчет](../../research/direct_trend_visibility/NATIVE_QA.md)

Оригиналният RC2 Strategy Tester архив има собствен краен момент, 11.11.2026, 06:00 UTC. Режимите `RC2 Fixed SL` и `S5 RAW Parity` съвпадат по CORE и READY. Те управляват стоповете различно. На M5 приключват по 8 кампании, с нетен резултат съответно $56.411 и $32.294. На M15 приключват по 2, с $12.330 и $10.177. M30 е без сделки. Проверката валидира регистрираните изпълнения, частични изходи и полета на емулатора отделно за всеки режим. UNIFIED е индикатор. [Оригиналните нативни CSV](../gold-pro-rc2/evidence/native-logs.zip) · [RC2 методика](../gold-pro-rc2/PERFORMANCE_REPORT.md)

### Ново нативно сравнение на UNIFIED срещу `203f9d8`

Експортирани са по два отделни журнала от TradingView за всеки интервал. Във всяка двойка съвпадат началото на заредената история, OHLC, индексите и всички проверявани записи. Индикаторните журнали покриват потвърдените затваряния от 11.11.2026, 00:00 до 17:30 UTC включително. Съпоставката за Direct Trend Shadow обхваща по 1 199 потвърдени свещи от заредената история. Всички редове за събития и регистър съвпадат точно в трите двойки. [Нови журнали и SHA-256 в машинния отчет](evidence/parity.json)

| Интервал | Начало на заредената история UTC | OHLC в индикаторния журнал | CORE / READY | S5 / RC2 съобщения | RAW PAPER събития / приключени кампании | Нетно R / DD | Shadow събития |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| M5 | 05.11.2026 13:50 | 211 | 42 / 6 | 44 / 38 | 32 / 10 | −2.00237566 / 3.29956814 | 530 |
| M15 | 23.10.2026 20:00 | 71 | 20 / 2 | 19 / 17 | 6 / 2 | −0.04255369 / 1.03082818 | 502 |
| M30 | 06.10.2026 19:00 | 36 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 483 |

Нативните Pressure стойности съвпадат по двойки за всички 318 записани свещи. Независимото пресмятане по OHLC потвърждава 202, 62 и 27 стойности съответно на M5, M15 и M30. Първите девет стойности на всеки интервал изискват свещи отпреди началото на Pressure експорта. На M15 са потвърдени шестте тестови състояния на затворена свещ, включително `SELL WATCH` при 11:45 UTC и `WAIT`, `CONFLICT`, SELL ретест и BUY контрапример в останалите проверени случаи. [Изчисления и пълни записи на състоянията](evidence/parity.json)

[SHA-256 на шестте нови експорта](evidence/native-log-manifest.json). Суровите нови Pine Logs остават локално; отчетът включва сравнените полета и реалните снимки.

### Повторение на оригиналния Strategy Tester

Непромененият RC2 Audit се компилира и изпълни нативно и в двата режима. Запазени са стандартни свещи, 100 000 USD, 0.175 USD комисиона на единица за всяка страна, 2 ticks slippage, изпълнение при следващата налична цена и 1 200 свещи. Показаният диапазон на всеки ред важи и за двата режима. Броят в Strategy Report включва частични изходи. Няма нова стратегия за UNIFIED; новият паритет на неговите симулирани изпълнения е в RAW PAPER таблицата по-горе.

| Интервал (край 11.11.2026) | RC2 Fixed SL: P&L / PF / DD / затворени | S5 RAW Parity: P&L / PF / DD / затворени |
| --- | --- | --- |
| M5, от 05.11.2026 | [−14.30 USD / 0.878 / 76.43 USD / 36](evidence/screenshots/M5-audit-fixed.jpg) | [−39.49 USD / 0.641 / 68.78 USD / 36](evidence/screenshots/M5-audit-s5-raw.jpg) |
| M15, от 23.10.2026 | [−11.55 USD / 0.805 / 59.39 USD / 10](evidence/screenshots/M15-audit-fixed.jpg) | [−13.71 USD / 0.769 / 59.39 USD / 10](evidence/screenshots/M15-audit-s5-raw.jpg) |
| M30, от 06.10.2026 | [0 сделки](evidence/screenshots/M30-audit-fixed.jpg) | [0 сделки](evidence/screenshots/M30-audit-s5-raw.jpg) |

[Структурирани резултати](evidence/native-strategy-report.json). Basic показва обобщените резултати, но ограничава пълния Strategy Report. Проверка на конкретните изпълнения в този одит остава от отделния архив със собствен краен момент. Сумите в USD от Strategy Tester и тези в R от RAW PAPER са различни проверки.

## Известия

В нативния диалог са налични `Any alert() function call` и отделното `RESEARCH · SELL BREAKOUT CONFIRMED`. Новите RC2 съобщения са валидирани срещу съответните S5 събития и READY планове. Проверени са 7, 12 и 1 Shadow намерения за известие съответно на M5, M15 и M30. Всяко съответства на потвърдено наблюдение на същата свещ и не носи полета за изпълнение. Тестът не е създавал сървърно известие и не потвърждава доставка в приложението. Проверен е шаблонът с източник, потвърдена посока, UTC време на затваряне и означение Research only, както и историческият payload. Правило или графика не е записвана в профила. [Динамично известие](evidence/screenshots/M15-alert-dynamic-dialog.jpg) · [RESEARCH SELL условие](evidence/screenshots/M15-alert-research-condition.jpg) · [Валидиране на payload](evidence/parity.json)

## Възпроизвеждане

От корена на хранилището създай временни QA копия. `baseline.pine` и `unified.pine` запазват 1 200 заредени свещи и включват журналите за S5, Shadow, RAW PAPER и Pressure. Двете M30 копия с `-wide` имат начало на журнала 06.11.2026 UTC. [Генератор](qa_probes.py) · [Сверка](qa_parity.py)

```text
python -B releases/gold-pro-unified/qa_probes.py <new-probe-directory> --m30-start 2026-11-06T00:00:00Z
python -B releases/gold-pro-unified/qa_parity.py --native-dir <tradingview-csv-directory> --output <parity-json>
python -B -m unittest discover -s tests -q
```

Зареди всяка двойка в еднаква нативна графика на съответния интервал. Експортирай Pine Logs в `baseline-M5.csv` и `unified-M5.csv`, с аналогични имена за M15 и M30. За фиксирания исторически изглед на M15 използвай `review-journal-1200.pine` и `unified-review-M15.csv`. Изпълнение на сверка без `--native-dir` пресъздава само доказателствата от изходния код и архивите. 189 локални теста преминаха успешно.
