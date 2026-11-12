# QIRVANTA Luminous Zones — нативен QA

**VERIFIED** в TradingView на OANDA:XAUUSD с обикновени свещи. Компилирани са оригиналът `eb0428a`, началният Luminous и финалният Luminous. Финалният код в Pine Editor е сверен по SHA-256 `dd5aeab3af0fafd1bdae30204a42d99d886eee5d60b4ba0a3a51fc87a51d1763`. [Оригинална компилация](evidence/compile-baseline.jpg) · [Финална компилация](evidence/compile-luminous-final.jpg)

В трите двойки нативни Pine Logs CSV файловете съвпадат **байт по байт**. И в двете версии са използвани UTC, `calc_bars_count=1200`, включен QA export и режим извън Historical Review. Съвпадат началният бар, OHLC, warmup, ID на събитията, времената, ценовите граници, състоянията и alert payload-ите. Статичната проверка потвърждава защитените блокове за изчисление, жизнен цикъл и известяване. [Нативни журнали](evidence/native-logs.zip) · [Паритет](evidence/parity.json)

| Рамка | Първо отваряне UTC | Последно затваряне UTC | Затворени свещи | Зонални събития | Alert пакети / събития |
| --- | --- | --- | ---: | ---: | ---: |
| M5 | 06.11.2026 02:40 | 12.11.2026 10:35 | 1199 | 242 | 75 / 83 |
| M15 | 26.10.2026 08:45 | 12.11.2026 10:30 | 1199 | 164 | 49 / 53 |
| M30 | 07.10.2026 08:00 | 12.11.2026 10:30 | 1199 | 234 | 74 / 80 |
| Общо | | | 3597 | 640 | 198 / 216 |

**VERIFIED** за визуалните промени в заснетите сценарии. В първия Luminous кадър десният акцент е слаб при пет бара проекция и значката за формиране стига до ценовия етикет. След една визуална поправка се виждат широкият полупрозрачен ореол, тънката граница и по-плътният акцент в ценовата лента. Акцентът е преместен до наблюдаваното затваряне. Точните цени са на втория ред на съкратения desktop етикет. В MOBILE цените са на отделни редове в таблото. [Начален Luminous](evidence/M30-light-initial.jpg) · [Финален Luminous](evidence/M30-dual-light-after.jpg)

| Сценарий от 11.11.2026 UTC | Premium `eb0428a` | Финален Luminous |
| --- | --- | --- |
| M5 03:40, BUY rejection | [Преди](evidence/M5-rejection-light-before.jpg) | [След](evidence/M5-rejection-light-after.jpg) |
| M5 04:30, припокриващи се BUY и SELL зони | [Преди](evidence/M5-overlap-light-before.jpg) | [След](evidence/M5-overlap-light-after.jpg) |
| M5 17:25, празно табло след expiry | [Преди](evidence/M5-empty-light-before.jpg) | [След](evidence/M5-empty-light-after.jpg) |
| M15 12:00, активна SELL зона | [Преди](evidence/M15-sell-light-before.jpg) | [След](evidence/M15-sell-light-after.jpg) |
| M30 12:00, две активни зони, светъл desktop | [Преди](evidence/M30-dual-light-before.jpg) | [След](evidence/M30-dual-light-after.jpg) |
| M30 12:00, две активни зони, тъмен desktop | [Преди](evidence/M30-dual-dark-before.jpg) | [След](evidence/M30-dual-dark-after.jpg) |
| M30 13:30, BUY инвалидация | [Преди](evidence/M30-invalidation-dark-before.jpg) | [След](evidence/M30-invalidation-dark-after.jpg) |
| M30 12:00, светъл MOBILE 390 × 844 px | [Преди](evidence/M30-mobile-light-before.jpg) | [След](evidence/M30-mobile-light-after.jpg) |
| M30 12:00, тъмен MOBILE 390 × 844 px | [Преди](evidence/M30-mobile-dark-before.jpg) | [След](evidence/M30-mobile-dark-after.jpg) |

В M5 примера цените **4045.430–4046.180** попадат и в двете зони. Ценовите етикети са от двете страни на припокриването. Лявата граница остава при известно затваряне. Значките използват свещта на събитието, а линиите и двете запълвания използват същите проксимална и дистална цена. Виждат се и текущият изглед и приближеният пример с граници. [Текущ M30](evidence/M30-live-observation.jpg) · [Приближен Premium](evidence/M30-rail-detail-before.jpg) · [Приближен Luminous](evidence/M30-rail-detail-after.jpg)

**VERIFIED** за `Luminous ON/OFF`, `Formation badges OFF`, `Show precision zones OFF`, `Larger text ON/OFF`, DESKTOP, COMPACT и MOBILE. Проекцията от 1, 3 и 5 бара мести десния край на лентата и етикета, а акцентът остава до наблюдаваното затваряне. На COMPACT двата панела се виждат едновременно на тестовата графика. [Проекция 1](evidence/M30-projection-1.jpg) · [3](evidence/M30-projection-3.jpg) · [5](evidence/M30-projection-5.jpg) · [Luminous OFF](evidence/M30-luminous-off.jpg) · [Formation OFF](evidence/M30-formation-off.jpg) · [Show OFF](evidence/M30-display-off.jpg) · [COMPACT с QIRV PREMIUM](evidence/M30-compact-with-qirv-premium.jpg)

При 390 px с малък десен отстъп етикетът върху графиката може да излезе извън платното. Таблото остава четимо. Достатъчен десен отстъп или по-къса проекция освобождава място за етикета. При силно сгъстена история отделни значки могат да се застъпят. [Кадър при тесен отстъп](evidence/M30-mobile-dark-tight-margin.jpg)

**BLOCKED** за доставка на техническо известие до iPhone при Basic. Нативният диалог показва Luminous и `Any alert() function call`. Сравнените 216 събития доказват паритет на payload-ите. Доставка до устройство не е наблюдавана. [Alert условие](evidence/alert-source.jpg) · [Лимити на TradingView](https://www.tradingview.com/pricing/)

`main`, S5/RC2 и запечатаният forward тест са запазени. Зоните остават за изследване.

Възпроизвеждане от корена на репото.

```sh
python -B -m zipfile -e research/luminous_zones/evidence/native-logs.zip luminous-native
python -B -m research.luminous_zones.qa --native-dir luminous-native --output luminous-parity.json
```
