# Нативен паритет · QIRVANTA GOLD PRO Directional Visibility

Целият файл `QIRVANTA_GOLD_PRO_SELL_VISIBILITY.pine` е компилиран в Pine v6 и добавен на TradingView. SHA-256 на текста в редактора и на доставения файл е `91fba94db0d71d2dd25fcaf72aedf34a7d7cb955d4362996b086ca07c05163d0`. Тестът използва отделен таб на тестовия профил, стандартни OANDA:XAUUSD свещи и изключен Autosave. Снимките са оригинални кадри от TradingView. [Компилация](evidence/screenshots/native-compile.jpg) · [Проверени файлове и отпечатъци](evidence/manifest.json)

## Затварянията на 11 ноември 2026 г.

| UTC · M15 | Посочно наблюдение | Разрешение | Нативен кадър |
| --- | --- | --- | --- |
| 00:15 | **BUY BREAKOUT CONFIRMED**, shadow ретест | WAIT | [BUY пример](evidence/screenshots/M15-0015-buy-detected-wait.jpg) |
| 11:30 | **NONE**. В четирите предходни свещи нетната промяна е 3,320 при диапазон 9,640 | WAIT | [Страничен прозорец](evidence/screenshots/M15-1130-sideways-none-wait.jpg) |
| 11:45 | **SELL WATCH**, shadow структура | WAIT | [SELL WATCH](evidence/screenshots/M15-1145-sell-watch-wait.jpg) |
| 12:00 | **CONFLICT**, S5 BUY WATCH и shadow структурен SELL. Shadow геометрията е потвърдена, а диагностичният план е отхвърлен за недостатъчно нетно място | WAIT | [Двата източника](evidence/screenshots/M15-1200-conflict-sell-detected-wait.jpg) |
| 12:30 | **SELL BREAKOUT CONFIRMED**, shadow ретест, **RESEARCH** | WAIT. RAW Paper е свободен на това затваряне | [SELL DETECTED + WAIT](evidence/screenshots/M15-1230-sell-detected-wait.jpg) |
| 13:30 | **CONFLICT**, S5 BUY WATCH и потвърден shadow SELL от консолидация | WAIT | [SELL DETECTED + CONFLICT + WAIT](evidence/screenshots/M15-1330-conflict-sell-detected-wait.jpg) |
| 15:30 | **CONFLICT** при мечи H1/H4. S5 BUY WATCH е означен **COUNTERTREND**, а структурният shadow SELL има собствен етикет | WAIT | [Мечи контекст и контратренд](evidence/screenshots/M15-1530-bearish-countertrend-conflict.jpg) |

Затварянето в 12:30 е изнесено отделно с включен **HISTORICAL REVIEW**. Снимката на състоянието съвпада с каноничния журнал по източници, състояния, времена, причини, разрешение и липса на активни ценови нива. Архивният Fixed SL Strategy Tester и RAW Paper имат различни правила за симулирано отваряне. В 12:30 индикаторът показва наблюдавания RAW Paper слот. [Машинна проверка](evidence/native-qa.json)

Нативните примери включват [валиден S5 BUY план с Entry/SL/TP](evidence/screenshots/M15-1215-valid-buy-plan.jpg), [реално зает RAW Paper слот](evidence/screenshots/M15-0230-paper-occupied.jpg) и [отхвърляне от S5](evidence/screenshots/M15-1300-s5-risk-rejected.jpg). В [LIVE PREMIUM M30](evidence/screenshots/live-M30-sell-detected-wait.jpg) се вижда потвърденият **SELL DETECTED** от 18:00 UTC и **WAIT** при наблюдавано затваряне в 19:00 UTC. Снимката показва текущия графичен прозорец без активни Entry/SL/TP. [LIVE M5](evidence/screenshots/live-M5-premium.jpg) · [LIVE M15](evidence/screenshots/live-M15-premium.jpg)

## Еднакви сигнали, изпълнения и P&L

Сравнението използва замразената база `bcda58bedf20d6dd626e49849b48c613746167f1` и поправения пълен индикатор при еднакви начални барове и край **17:30 UTC на 11.11**. Прозорецът на RC2 и RAW Paper журналите започва при затварянето в **00:00 UTC**. Shadow събитията са сравнени по цялата заредена история от **1 199 затворени бара на интервал**. Всички ценови полета, времена, причини и ред на събитията съвпадат.

| Интервал | Затваряния | S5 CORE / READY / съществуващи alert събития | RAW Paper събития | Нетен RAW Paper P&L в R | Shadow събития |
| --- | ---: | --- | ---: | ---: | ---: |
| M5 | 211 | 42 / 6 / 44 | 32 | −2,00237566 | 532 |
| M15 | 71 | 20 / 2 / 19 | 6 | −0,04255369 | 499 |
| M30 | 36 | 0 / 0 / 0 | 0 | 0 | 493 |

За M30 няма S5 изпълнения в този прозорец. Паритетът на M30 обхваща съвпадащия контекст, състояния и shadow събития. Отделната защитена стратегия **RC2 Fixed SL** е проверена по оригиналния SHA-256. В машинния отчет са приложени предишните нативни Strategy Tester резултати на M5 и M15. Новото сдвоено изпълнение е за индикатора и неговия RAW Paper журнал. [Изходни нативни CSV](evidence/native-logs.zip) · [Предишен нативен Fixed SL отчет](../direct_trend_breakout/evidence/base-native.json)

Опционалните RESEARCH условия се виждат отделно в TradingView, заедно с оригиналното **Any alert() function call**. Native QA потвърди **20** наблюдателни събития за потенциално известяване. TradingView показва общо предупреждение в диалога за известия. Създаване и доставка на известие не са тествани. [Двете условия](evidence/screenshots/observational-alert-conditions.jpg) · [SELL шаблон](evidence/screenshots/observational-sell-alert-template.jpg)

## Възпроизвеждане

```text
python -B unified/tools/build_direct_trend_visibility.py indicator --check
python -B -m unittest discover -s tests
python -m zipfile -e research/direct_trend_visibility/evidence/native-logs.zip visibility-native
python -B -m research.direct_trend_visibility.native_qa --native-dir visibility-native --output visibility-native-qa.json
```

За ново нативно сравнение генерирай временния контрол с `build_direct_trend_visibility.py baseline path/to/baseline.pine`. При поправения файл включи QA журналите на RC2 и shadow, shadow визуализацията и RESEARCH известията през Inputs. За валидно сравнение двата експорта на всеки интервал трябва да започват от една и съща заредена свещ. Параметърът `--bars` на временния probe позволява изравняване при изместена загрявка.
