# Visual V2 — нативна проверка

**VERIFIED** за пълния [Pine v6 индикатор](QIRVANTA_GOLD_PRO_VISUAL_V2.pine), изследвания прозорец на M5/M15/M30 и заснетите desktop/MOBILE изгледи. Контролата е `26ad1aa2992f9e2e7e28c13fbbd3f321e1de8261`.

## Коригирани дефекти

- На M5, 11 ноември 2026, 09:45 UTC, големият SELL етикет на изходния V2 закриваше TP2 на BUY плана и стигаше под таблото. Когато зоната попада във визуалния диапазон на плана, нейният етикет сега е вляво, а Entry, SL и трите TP остават отдясно. Ценовите линии, границите на зоната и барът на сигнала са непроменени. Зоналните значки са по-компактни.
- При 390 px заглавието `DIRECTIONAL OPPORTUNITY` се изрязваше с по-едрия V2 шрифт. MOBILE отново ползва компактния размер, а краткият, обърнат наляво зонален етикет показва целия ценови диапазон. Desktop запазва Large.
- Контрастът и подравняването са проверени и на светъл фон. При силно сгъстен desktop изглед дълъг зонален надпис без S5 план все още може да се доближи до таблото; четимостта зависи от мащаба. Мобилните кадри са с WAIT и зона, без активен S5 план. MOBILE скрива десните ценови етикети на плана по наследеното правило; при валиден план цените са в таблото.

## Компилация и паритет

Точният краен източник (`SHA-256 77feedcbef84e77b9270ec43840be5b823d838300d0a09d5ddb4fec7acc089f4`) беше зареден и [компилиран нативно в TradingView](evidence/M5-v2-final-compiled.jpg). Последваха нативни изчисления на M15 и M30 със същия компилиран файл. Не е наблюдаван compiler timeout или runtime грешка. Проверката беше във временен таб с изключен Autosave.

Двата индикатора работиха върху **една графика** на `OANDA:XAUUSD`, обикновени свещи, с еднакви настройки за наблюдения, S5/RC2 и RAW PAPER. Общият одитен интервал е **11.11.2026, 00:00–17:30 UTC**, включително крайните потвърдени затваряния. За всяка рамка съвпадат 1222 потвърдени OHLC бара до общия `DT_END`, включително warmup. Първият опит в два таба показа две разлики във върховете на M5 свещи във warmup; този опит не е използван за паритет. В крайния M5 експорт има едно допълнително потвърждение в кандидата на 12.11.2026 в 20:15 UTC, след общия `DT_END` същия ден в 20:10 UTC. M15 и M30 експортите са байт по байт еднакви.

| Рамка | Първи зареден бар (UTC) | Затваряния в одита | CORE / READY / RAW събития | Зонални събития | RAW PAPER нетно R | Макс. спад (R) |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| M5 | 06.11.2026 10:20 | 211 | 42 / 6 / 32 | 66 | −1.23099353 | 2.98907829 |
| M15 | 26.10.2026 12:45 | 71 | 20 / 2 / 6 | 13 | −0.04255369 | 1.03082818 |
| M30 | 07.10.2026 06:00 | 36 | 0 / 0 / 0 | 7 | 0 | 0 |

Всяко сравнено S5/RC2 събитие съвпада по съдържание и ред. Проверени са READY, време, източник, Entry/SL/TP1–TP3, разрешенията за риск, моделираните входове и изходи, заетостта, разходите и P&L в R. Моделирани разходи: 0.75063479R на M5 и 0.05380326R на M15. При M15 има един идентичен отказ по капацитет. M30 няма изпълнения в избрания прозорец; там паритетът не включва пример за изпълнена сделка. Съвпадат и всичките **86 LZ_EVENT** реда, включително формиране, първо докосване, rejection, invalidation, mitigation и expiry, както и наблюденията Shadow и Pressure в режим OHLC.

Паритетът на alerts е за нативните payload-и и намерението за известяване в Pine Logs. Не е създавано активно известие и не е проверявана доставка. P&L е от нативния журнал **RAW PAPER** на индикатора, в R; няма брокерски изпълнения. Тегленето по tick volume не е упражнено в тази сверка.

Статичният одит потвърди Pine v6 декларацията, ограниченията на обектите (60 labels, 26 lines, 12 boxes), една таблица, без нови заявки за данни, alert извиквания или поръчки. Изчисленията на S5/RC2, RAW PAPER и зоналният lifecycle остават същите. Зоните остават `RESEARCH ONLY`. Локалният набор: **189 теста, всички успешни**.

## Нативни кадри

Desktop: 1543 × 883 px. MOBILE: измерен браузърен viewport **390 × 844 px**. Историческите кадри са от 11.11.2026 UTC. „Преди“ е `26ad1aa`. Единствено M5 светъл „след“ е от междинния компилиран V2, чийто desktop изглед е идентичен с крайния файл; последвалата поправка е само за MOBILE шрифта.

| Случай | Преди | След |
| --- | --- | --- |
| M5 09:45 — BUY READY, SELL зона до TP2, тъмен | [26ad1aa](evidence/M5-buy-desktop-dark-before.jpg) | [V2](evidence/M5-buy-desktop-dark-after.jpg) |
| M5 09:45 — същият BUY план, светъл | [26ad1aa](evidence/M5-buy-desktop-light-before.jpg) | [V2](evidence/M5-buy-desktop-light-after.jpg) |
| M5 00:25 — SELL READY, BUY зона, тъмен | [26ad1aa](evidence/M5-sell-desktop-dark-before.jpg) | [V2](evidence/M5-sell-desktop-dark-after.jpg) |
| M5 03:40 — реакция в BUY зона, тъмен | [26ad1aa](evidence/M5-rejection-desktop-dark-before.jpg) | [V2](evidence/M5-rejection-desktop-dark-after.jpg) |
| M15 12:00 — SELL DETECTED и WAIT, тъмен | [26ad1aa](evidence/M15-sell-desktop-dark-before.jpg) | [V2](evidence/M15-sell-desktop-dark-after.jpg) |
| M30 — BUY и SELL зони, светъл | [26ad1aa](evidence/M30-dual-desktop-light-before.jpg) | [V2](evidence/M30-dual-desktop-light-after.jpg) |
| M30 MOBILE — SELL зона, тъмен | [26ad1aa](evidence/M30-mobile-dark-before.jpg) | [V2](evidence/M30-mobile-dark-after.jpg) |
| M30 MOBILE — SELL зона, светъл | [26ad1aa](evidence/M30-mobile-light-before.jpg) | [V2](evidence/M30-mobile-light-after.jpg) |

Кадри на дефектите преди поправката: [V2 етикет върху TP2](evidence/M5-buy-desktop-dark-original-v2.jpg) и [V2 MOBILE с отрязан заглавен ред](evidence/M30-mobile-dark-v2-wide-font.jpg).

## Възпроизвеждане

От корена на репозиторията, с наличен git обект `26ad1aa`:

```sh
python -B -m research.luminous_visual_v2.audit_source --output source-parity.json
python -B -m zipfile -e research/luminous_visual_v2/evidence/native-pairs.zip native-pairs
python -B -m research.luminous_visual_v2.compare_native native-pairs --output native-parity.json
python -B -m unittest discover -s tests
```

[Статичен отчет (JSON)](evidence/source-parity.json) · [Нативен отчет (JSON)](evidence/native-parity.json) · [Оригинални Pine Logs (ZIP)](evidence/native-pairs.zip)
