# QIRVANTA — доставка от audited baseline

Начало: commit `103bc30788b18234f5938ed0f5346e142097a6fe`, клон `qirvanta-refactored-delivery`. Поправките са подготвени в `fix/audited-validation-integrity` за поисканата GitHub публикация; текущ статус PENDING. Main не е променен; [публикационни данни](PUBLICATION_BG.md).

**Причината за наблюдавания отказ на VALIDATION е възпроизведена:** началният файл включва High detalization (`use_bar_magnifier=true`), а TradingView го отказва с изискване за Premium. На OANDA:XAUUSD H1, стандартни свещи, същият код заработи след превключване към Default detalization. Затова текущият native header използва `use_bar_magnifier=false`. Първият опит преди вход в профила беше блокиран от Log in; това е историческо ограничение, вече преодоляно. Точният обхват на проверката на окончателните файлове е в [проверки](VALIDATION_BG.md) и `reports/ACCEPTANCE.json`.

## Използване

- `QIRVANTA_SNIPER_PRO.pine`: самостоятелен индикатор за графиката и информационни известия.
- `QIRVANTA_SNIPER_VALIDATION.pine`: самостоятелна стратегия само за Strategy Tester. Поставя виртуални поръчки в emulator, не свързва MT5.
- Копирай целия съответен файл в отделен Pine Editor документ. Използвай стандартни свещи и M5/M15/M30/H1/H4. На M30 автоматичният хоризонт е суинг; ранното обръщане работи само M5/M15/M30.
- Изгледът е МОБИЛЕН по подразбиране: пет основни реда. СТАТИСТИКА и ДИАГНОСТИКА заменят таблото. Подробните исторически надписи са изключени; малките потвърдени стрелки остават.

В тази сесия TradingView Basic показва лимит **1 запазен личен скрипт**. По избор на потребителя PRO е записан като личен `QIRVANTA_SNIPER_PRO`, версия 2, на мястото на наличния скрипт. След Save и reload на layout PRO остава видим, а VALIDATION е запазен като скрит chart instance на M15, без втори личен script slot. За показване на стратегията/Strategy Report включи окото (Show) до QIRV TEST; отделният Pine файл също е включен. И двата окончателни файла са компилирани и изпълнени на OANDA:XAUUSD Candles, M5/M15/M30/H1/H4 (проверка за зареждане и изчисляване без наблюдаван runtime отказ); компилацията на PRO има две разгледани предупреждения CW10003 (подробности във VALIDATION_BG). Окончателният VALIDATION автоматично използва Default detalization и показва същите H1 native обобщени резултати като baseline при Default за видимия период 02.01–06.11.2026. Това е ограничено UI сравнение, не пълно журнално съгласуване или OOS доказателство. Ограничението на записването е различно от runtime отказа за High detalization. Стандартната детайлност променя native fill модела, не сигналните филтри; за сравнение на версии използвай еднаква детайлност и разходи.

Проверени са действителни тесни viewport-и 390×844 и 430×932: таблото горе вдясно застъпва chart header. Това е отворен layout дефект (`MOBILE_UI: PARTIAL_FAIL_LAYOUT`), не mobile PASS. Позиция долу вдясно е възможна настройка за проба, но не е удостоверена като поправка в тези снимки.

WATCH / „СЛЕДИ НАГОРЕ/НАДОЛУ“ означава подготовка, **не вход**. „ПОТВ.“ означава технически условия на затворена свещ; „ЧАКА OPEN“ още няма виртуално изпълнение. „ВХОД СИМ.“ е цена от симулацията, не от реалната сметка.

Панелът показва ориентир/симулиран вход, първоначален структурен SL, отделен текущ симулиран SL при промяна и TP1–TP3. **TP1–TP3 са R проекции от цената на сигнала и първоначалния риск**, не установени структурни цели. „Преграда“ е отделно потвърдено ниво (при обръщане може да е EMA), не автоматична цел за изход. „—“ означава неустановена преграда. Структурният стоп включва ATR буфер, минимална ATR дистанция и закръгляне навън. [Точните условия и числов пример](docs/SIGNAL_RULES_BG.md).

## Известия

Включи общия ключ и желаните категории в група 07. За пълния JSON с цени избери „Any alert() function call“ / „alert() function calls only“. `alertcondition` е само в PRO; native order-fill alerts са изключени. Известията се обработват на затваряне, не обещават моментално intrabar известяване. Не конфигурирай брокерски webhook. След промяна на код/настройки пресъздай server-side известията в TradingView. Новинарски календар не е свързан; паузата е ръчна.

## Източник и възпроизвеждане

Последните audited файлове бяха публикувани без техните src/tests. Тук src е възстановен **от тях**, с доказано точно възпроизвеждане преди поправките. Сигналното/PAPER ядро е запазено с SHA256 `c189ef479ccc106178a8a3b146da0dcd7d2fac852ddbeb8eebb298f385748dcc`. По-старият v7.6 е само исторически архив.

Python 3.11+; на Windows е нужен пакет `tzdata`, ако не е инсталиран. Командите се изпълняват от `refactored/`:

```sh
python tools/build.py --check
python tools/build.py
python tools/run_tests.py
python tools/compare_events.py baseline_events.jsonl fixed_events.jsonl --out reports/event_comparison.json
python tools/compare_runs.py --bars ohlc.csv --baseline-events baseline_events.jsonl --fixed-events fixed_events.jsonl --baseline-manifest baseline_manifest.json --fixed-manifest fixed_manifest.json --out reports/paired_run
python tools/package.py --out QIRVANTA_DELIVERY.zip
```

`build.py` записва еднакви outputs в refactored и audited; `--check` проверява байтовете без запис. ZIP поддържа тази структура. `baseline/delivery_103bc307.zip` пази целия начален commit; `baseline/audited/` пази точните начални Pine байтове. Старите отчети са в този архив. Актуални са `reports/ACCEPTANCE.json`, `reports/tests.json` и `reports/unit_tests.log`.

Коментарът `PINE_COMPILE_UNVERIFIED` в генерираните Pine файлове е статичен build banner; последващата действителна компилация за точните SHA е удостоверена в `reports/tradingview_attempt.json`. Кодът не е променян само за обновяване на коментара.

Без реални OHLC и пълни Pine event logs: `PERFORMANCE_UNVERIFIED_MISSING_DATA`. Python PASS не е Pine compilation, native parity, OOS или готовност за реална търговия.
