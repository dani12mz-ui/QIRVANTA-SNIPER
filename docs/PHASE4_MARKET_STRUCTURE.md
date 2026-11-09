# Фаза 4 — Структура, сесии и измерими ценови характеристики

`LOCALLY_TESTED`. Библиотеката разширява замразените причинни EMA, ATR, ADX, RSI, pivots, H1/H4 и сесии. Всеки row има availability timestamp от close. Потвърдените 3/3 swing стойности могат да служат за граница от следващата свещ. Prefix проверката и gap тестовете са изпълнени. [Причинни характеристики](../research/ultimate/structure.py) · [Регресионни тестове](../research/tests/test_ultimate_research.py).

| Компонент | Оперативна дефиниция | Ниво |
|---|---|---|
| Потвърден swing | Строг high или low спрямо три свещи от всяка страна, без gap. Става наличен на третия следващ close | Наблюдаван OHLC |
| BOS / CHoCH / MSS | Close пресича последния предварително потвърден swing. CHoCH сменя последната BOS посока. MSS изисква CHoCH с подписано тяло ≥0,60 ATR | Ценови proxy класове |
| Sweep / swing failure | High/low пробива известен swing и close се връща. S9 изисква отделно потвърждение | Наблюдавано ценово събитие |
| Equal highs/lows | Два потвърдени еднопосочни swing extrema на разстояние ≤0,12 ATR | Ценово приближение |
| Displacement / FVG | Подписано тяло / ATR. Bull FVG при low > high преди две свещи, bear огледално. Тройката е непрекъсната | Наблюдаван OHLC |
| Order block | Последната противоположна свещ в до три непрекъснати предходни бара, открита при BOS с тяло ≥0,60 ATR | Цена proxy |
| Premium / discount | Close над/под средата на предходните потвърдени swing граници | Цена proxy |
| Предишни ден и седмица | High/low на предходния наблюдаван UTC интервал. Първият интервал може да е непълен | Наблюдаван архив |
| Азия | UTC 00:00–06:00, валиден при всички свещи от начало до край. Публикува се след 06:00 | Дефиниран сесиен прозорец |
| London / NY opening | Първи 30 минути след 08:00 London и 09:30 New York, IANA DST и непрекъснати барове | Дефиниран сесиен прозорец |
| Failed auction / acceptance | F изисква wick отвъд 20-bar extreme и reclaim. D използва затворен предходен пробив на opening range, после reclaim. J изисква втори close ≥0,10 ATR зад замразена 20-bar граница | Цена proxy |
| Компресия и експанзия | ATR/40-bar ATR под 0,75 или над 1,50 в базовия режим. J използва фиксиран праг 1,35 | Цена proxy |
| Повторни sweep / cascade | Поне два same-side sweep в последните 5 затворени непрекъснати наблюдения. Няма свързани order-flow observations | Цена proxy |
| Място до цел | Най-близък известен swing, 20-bar extreme или предишен UTC дневен/седмичен extreme в посоката. Изискване за ≥1R се изолира в L | Цена proxy |
| Pullback depth / trend maturity | Подписана дълбочина спрямо известния swing range и брой последователни барове в последната BOS посока, рестарт при gap | Цена proxy |
| VWAP / Volume Profile | S6 има strict source/volume gate. Точен trade volume и разпределение по цена липсват в архива | BLOCKED_EXTERNAL |
| Real delta, cumulative delta и absorption | Нужни са валиден trade/tick feed и идентификация на агресора | BLOCKED_EXTERNAL |
| Cross-asset divergence | Нужен е съгласуван point-in-time втори инструмент. BTC/PU Prime остава без потвърдена история тук | BLOCKED_EXTERNAL |

Библиотеката регистрира 553 BOS, 186 CHoCH, 135 MSS, 382 горни и 406 долни sweep, 1 376 трисвещни FVG и 322 order-block proxy събития върху M15 архива. Част от условията се припокриват. Chart overlays от Pine baseline и режимите им са в Phase 1. [Броячи и определения](../research/ultimate/reports/phase4_structure.json) · [Платформена проверка](PHASE1_ENGINEERING.md).

При изолирания room gate L на пълния M15 архив резултатът се изменя от 377/−431,865 до 260/−74,258 USD. На общия прозорец M5 се изменя от 169/+43,370 до 117/+278,186. На M15 се изменя от 49/−45,472 до 29/−70,709, а на M30 от 24/+262,865 до 18/−217,167. Еднаквите OHLC допускания и разходи отделят конкретната промяна, но заетостта се изменя. Противоположните посоки по рамки оставят избора `INSUFFICIENT_EVIDENCE`. [Сравнения на общ прозорец](PERFORMANCE_COMPARISON.md).

## Локален структурен изглед

SVG renderer изобразява последните 80 затворени M15 свещи от pinned архива, известните swing нива, BOS/CHoCH/MSS, FVG proxy и завършения азиатски диапазон. Етикетите и областите започват при close на свещта, в която информацията става достъпна. Прекъсванията са маркирани, а хоризонталното разстояние между свещите е еднакво. Новите слоеве са проверени локално в SVG. За тях няма отделен Pine порт. Две изпълнения върху действителния архив дадоха идентичен SHA-256 и валиден XML с 18 FVG и 7 структурни маркера. Самият графичен файл остава локално. [Renderer](../research/ultimate/overlay.py) · [Проверка](../research/ultimate/overlay_verification.json).

```powershell
python -B -m research.ultimate.overlay --output ../ultimate_structure_M15_new.svg
```
