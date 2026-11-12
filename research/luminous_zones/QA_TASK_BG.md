# QIRVANTA LUMINOUS ZONES — визуален кандидат

**Статус: SOURCE-LEVEL CHECK PASS / TRADINGVIEW NATIVE QA NOT YET RUN.**

Тази версия е **непосредствена визуална поправка**, а не нов модел. Използва оригиналния **нативно проверен** Precision Zones Premium от commit `eb0428a13e626ec233bed85752e81028890e2d8f` (с 650 проверени събития и 220 alert събития). В новия клон все още НЕ са потвърдени компилация и снимки от TradingView.

## Какво вече е променено

1. **Двойни luminous ценови граници** на всяка активна зона — широка полупрозрачна линия под тънка ясна линия; еднакви реални проксимална и дистална цена, без подправени ценови разширения.
2. **Two-layer zone fill** — основна полупрозрачна зона плюс по-наситен акцент вдясно. Вътрешният акцент остава изцяло ВЪТРЕ в реалния диапазон; не прогнозира следващо движение.
3. **Formation / rejection / invalidation badges**, които се създават на действителната затворена свещ. За touch остава малък маркер, а при rejection се вижда ясно `✓ BUY REJECTION` / `✓ SELL REJECTION`.
4. **Пълен ценови етикет върху графиката**, до зоната, със статус и ценовите граници; в MOBILE остава кратка форма, точните цени са в таблото/tooltip.
5. **По-едър табличен ценови ред**, с отделни BUY/SELL карти и статус `●`.
6. Визуални Inputs: `Luminous zone borders` (по подразбиране ON), `Zone right projection (bars)` (от 1 до 8), `Formation event badges` (ON). Проекцията е само графично продължение на вече известна зона; не е предсказана цена.

### Реални ограничения на Pine

TradingView Pine **не е CSS / HTML двигател**. Няма истински blur, drop-shadow, произволни шрифтове, анимирани SVG ефекти или динамичен responsive layout в таблица. Тук `luminous` е **псевдо-светене от два слоя реални линии**, което Pine поддържа. Не заявявай истински анимации или градиенти, ако не са визуално реализирани.

## Какво гарантира изходният diff, но не нативният компилатор

- Обработката между `if pzBar` и секцията за визуализация е идентична с оригинала: времеви подписи, pivot, зона, touch/rejection, invalidation, expiry, Pine Logs и alert събития НЕ са променяни.
- Броят на `alert()` остава 1. Няма `request.*`, няма `strategy.*`, няма брокерски поръчки.
- Графичните лимити са 4 `box` и 8 `line` — по две полупрозрачни кутии и четири линии на зона, всички многократно използвани, не се натрупват безкрайно.
- Допълнителните formation badges са *само drawing*: не добавят сигнално събитие или alert. Пулът продължава да задържа до 12 исторически графични маркера.

## Задължителна QA команда за Codex — едно изпълнение

**Repo:** `dani12mz-ui/QIRVANTA-SNIPER`
**Branch:** `research/qirvanta-luminous-zones-visual`
**Candidate:** `research/luminous_zones/QIRVANTA_LUMINOUS_ZONES.pine`
**Baseline:** `research/precision_buy_sell_zones_premium/QIRVANTA_PRECISION_ZONES_PREMIUM.pine` @ `eb0428a`

1. Прочети целия код и направи Pine v6 статична проверка на новите `line`, `box` и Unicode badges.
2. Нативно компилирай и добави **точно candidate файла** на TradingView `OANDA:XAUUSD` M5/M15/M30, стандартни свещи. Ако компилацията не успее, поправи само UI частта; не променяй zone-event engine.
3. В еднакъв `calc_bars_count=1200`, история, timezone и настройки включи QA export за оригинала и кандидата. Сравни byte/exact event payload, ID, timestamps, prices, states, all alert intents. Старите резултати 248/168/234 zone events са ориентир за конкретния архивен прозорец, не задължителен брой при нови пазарни барове.
4. Направи нативна **галерия преди/след** за M5 rejection, M15 активна SELL зона, M30 две зони, invalidation, празен panel, светъл/тъмен фон и 390×844 px.
5. Докажи чрез реални screenshots, че **двойните luminous rails и вътрешният акцент** са видими и изглеждат значително по-добре. Ако даден ефект не се вижда, настрой само дебелина/алфа/разположение, а не правилата.
6. Провери label clipping, zone price scale, right margin, overlapping same-price zones, desktop zone projection 1/3/5, `Show precision zones OFF`, `Luminous OFF`, `Formation badges OFF`, Mobile, Larger text ON/OFF.
7. В MOBILE `Top right` панелът не трябва да се реже под status line. Използвай реално 390px browser viewport; на DESKTOP/COMPACT с основния QIRVANTA индикатор мести таблото така, че да не закрива неговия сигнален панел.
8. Не допускай маркер, известен едва при затварянето, да се рисува на предишна свещ. Историческите и live зони трябва да спазват оригиналната causal логика.
9. Ако TradingView Basic блокира добавянето на втори индикатор или alerts, **не купувай план**; маркирай съответното като BLOCKED. Няма право да обявиш iPhone доставка само по Pine Logs.
10. Публикувай screenshots, `QA_BG.md`, native logs и един **QA commit** само в този branch. Ако новата композиция не е видимо по-професионална, направи един визуален refinement pass и повтори QA, без нови стратегии.
11. Запази `main`, S5/RC2 и запечатания forward тест неизменени. Всички зони остават `RESEARCH ONLY`.

**Цел:** видима, качествена и четлива premium зона, не три пъти по-голям код и не неподкрепено твърдение за печалба.
