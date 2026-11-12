# Нативен QA на Precision Zones Premium

**VERIFIED.** TradingView компилира оригинала `4059963` и финалния Premium Pine v6. Финалният файл в Pine Editor е сверен по SHA-256 `124db7b8324cd1070a8f380372501d28c654dd965003cf11b3250a4ca818cd88`. Използвани са стандартни свещи OANDA:XAUUSD във временни табове на тестовия профил. [Оригинална компилация](evidence/compile-original.jpg) · [Финална компилация](evidence/compile-premium-final.jpg)

| Рамка | Начало на заредената история UTC | Последно затваряне UTC | Свещи | Събития | Alert пакети / събития |
| --- | --- | --- | ---: | ---: | ---: |
| M5 | 06.11.2026 02:15 | 12.11.2026 10:10 | 1199 | 248 | 77 / 85 |
| M15 | 26.10.2026 08:15 | 12.11.2026 10:00 | 1199 | 168 | 51 / 55 |
| M30 | 07.10.2026 07:30 | 12.11.2026 10:00 | 1199 | 234 | 74 / 80 |

Трите двойки CSV съвпадат байт по байт. Това покрива 650 събития с идентични ID, UTC времена, ценови граници, състояния и alert payload. Съвпадат пазарните входове, началният бар и последният затворен исторически бар. Непроменени са Pine блоковете за откриване, touch, rejection, invalidation, expiry, журнал и alerts. SHA-256 проверките потвърждават оригиналния индикатор, протокола, защитения QIRVANTA и forward печата. [Шестте нативни CSV файла](evidence/native-logs.zip) · [Машинно четим паритет](evidence/parity.json)

**VERIFIED.** На desktop етикетът на по-високата зона се изнася над лентата, а този на по-ниската под нея. Маркерът за инвалидация е по-контрастен и се рисува от противоположната страна на свещта спрямо rejection за същата посока. Малката точка за touch е при wick на свещта. Лявата граница на зоната е потвърждаващото UTC затваряне, а маркерите остават на свещите на събитията. [Първоначално застъпване на desktop](evidence/M30-desktop-light-initial-premium.jpg)

В MOBILE ценовите диапазони остават в двете колони на таблото. Етикетите на графиката показват посока и кратък статус, а точните цени са достъпни и в подсказките. `Auto` оставя desktop таблото долу вляво и поставя MOBILE таблото горе вдясно, под статус реда. При силно сгъстена история или при включени маркери от друг индикатор отделни исторически етикети могат да се застъпят. [Първоначално застъпване при 390 px](evidence/M30-mobile-initial-premium.jpg)

| Проверен изглед на 11.11.2026 UTC | Оригинал | Финален Premium |
| --- | --- | --- |
| M5 03:40, BUY rejection | [Преди](evidence/M5-desktop-light-before.jpg) | [След](evidence/M5-desktop-light-after.jpg) |
| M15 12:00, активна SELL зона | [Преди](evidence/M15-desktop-light-before.jpg) | [След](evidence/M15-desktop-light-after.jpg) |
| M30 12:00, две активни зони | [Преди](evidence/M30-desktop-light-before.jpg) | [След](evidence/M30-desktop-light-after.jpg) |
| M30 12:00, MOBILE 390 × 844 px | [Преди](evidence/M30-mobile-light-before.jpg) | [Светло](evidence/M30-mobile-light-after.jpg) · [Тъмно](evidence/M30-mobile-dark-after.jpg) |
| M30 13:30, BUY инвалидация | | [Тъмен desktop](evidence/M30-desktop-dark-invalidation.jpg) |
| M5 17:25, празен панел след expiry | | [Кадър](evidence/M5-empty-after-expiry.jpg) |

**VERIFIED.** `Show precision zones = OFF` скри съществуващите ленти, маркери и таблото. При повторно включване обектите се появиха. Проверени са DESKTOP, COMPACT, MOBILE и Larger text ON/OFF. COMPACT се изобрази заедно с наличния QIRV PREMIUM в рамките на двата слота. [Изключен дисплей](evidence/M30-display-off.jpg) · [COMPACT с QIRV PREMIUM](evidence/M30-compact-with-existing.jpg)

**INCONCLUSIVE за доставяне на известие.** Сверени са 202 нативни пакета с 220 събития в Pine Logs. Сървърно изпращане и телефонно получаване са извън тази проверка.

Възпроизвеждане от корена на репото.

```sh
python -B -m zipfile -e research/precision_buy_sell_zones_premium/evidence/native-logs.zip zone-premium-native
python -B -m research.precision_buy_sell_zones_premium.qa --native-dir zone-premium-native --output zone-premium-parity.json
```
