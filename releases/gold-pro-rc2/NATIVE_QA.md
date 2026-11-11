# QIRVANTA GOLD PRO RC2 · нативна проверка

**Статус `PARTIALLY_COMPLETE`.** На 11.11.2026 г. пълният индикатор и RC2 Audit се компилираха в Pine v6, добавиха се към стандартни свещи на `OANDA:XAUUSD` и изчислиха M5, M15 и M30 без наблюдавана runtime грешка. Използвах отделния TradingView тестов профил във вградения браузър с изключен Autosave. [Индикатор](screenshots/01-indicator-compiled.jpg) · [Audit](screenshots/11-audit-compiled.jpg)

| Зареден Pine източник | SHA-256 |
| --- | --- |
| RC2 Premium | `20551f9e06a4997257dcae035a6ec20c9bd93d998dd79c5dc6276edd47969de1` |
| RC2 Audit | `4ba04321a9ef5456e8fc61aa2b5b3ab6c44398a62b18e31b3041d9dda530d42b` |
| RC1 Audit | `dfb154e407d32cbfc90ac957788b54cfa5b4e2034421b4b3ac26abf039d59278` |
| Premium V2 S5 QA probe | `7ac77a1e823e4ab2ad80d21414853b96241dc4b70ed2e005218ca236f8893285` |

Сглобяването проверява хешовете на двата публикувани RC1 файла. S5 референцията идва от `QIRVANTA_SNIPER_PREMIUM_V2.pine` с SHA-256 `a920f15370b536f19def0205b21915e1a3d2e4ed4c76c80bd22f5e8aa6bf7c10`. Нативните поръчки в RC2 Audit използват същите параметри за 100 000 USD капитал, 3 × 1 единици, 0,175 USD на единица на изпълнение, 2 тика slippage, следващ наличен тик и изключен Bar Magnifier. Отделният OHLC replay оценява неблагоприятния ред на SL/TP и допълнителния spread. [Данни и резултати](PERFORMANCE_REPORT.md) · [TradingView broker emulator](https://www.tradingview.com/pine-script-docs/concepts/strategies/)

## Паритет на сигнали, нива и изпълнения

Контролният край на Pine Logs е **11.11.2026 г. 06:00 UTC**. При повторно зареждане с еднаква първа свещ независимият S5 probe, RC1 Audit и RC2 в режим `S5 RAW Parity` съвпадат по OHLC, наличния H1/H4 контекст, `CORE`, `READY` и `NATIVE` записите. Индикаторът и първият RC2 Audit експорт съвпадат по CORE/READY върху общите проверени свещи. В режима `RC2 Fixed SL` същите сигнали и начални нива водят до различни изходи, защото началният стоп се запазва след TP1. [Сравнения и хешове на експортите](evidence/native-qa.json)

| TF | Еднакви свещи при повторния тест | S5 CORE | READY | Нативни събития |
| --- | ---: | ---: | ---: | ---: |
| M5 | 1 115 | 72 | 10 | 66 |
| M15 | 1 171 | 54 | 3 | 16 |
| M30 | 1 185 | 0 | 0 | 0 |

Първият S5 probe беше зареден с една свещ по-късно от първия RC1/RC2 Audit експорт. Този опит остава в данните. M5 се различава по два наблюдателни записа, `SELL WATCH` на 09.11 в 11:20 UTC и `EXPIRED` в 11:40 UTC. `READY` и нативните сделки съвпадат и в този опит. Повторението с еднаква първа свещ възпроизведе всички записи и показва защо началото на заредената история е част от тестовата конфигурация.

Проверени са UTC часовете на потвържденията, наличността на H1/H4 към решението, цените по плановете и нативните идентификати на изпълненията. Журналите на четирите политики не съдържат дублиран вход, TP след SL или връщане на стопа назад. Нативните BE и +0.25R събития са наблюдавани промени, насрочени след затварянето за следващия тик. H1 е контекстна рамка и е извън обхвата на входовете на фиксирания S5.

## Снимки от TradingView

- [M5 светла тема](screenshots/02-m5-light-premium.jpg) · [Текущ M5 BUY READY](screenshots/03-m5-live-buy.jpg) · [Текущ M30 WAIT](screenshots/04-m30-current-wait.jpg)
- [M5 исторически SELL и риск/печалба](screenshots/06-m5-sell-historical-risk-reward.jpg) · [M5 WATCH](screenshots/07-m5-watch.jpg) · [M5 INVALIDATED](screenshots/08-m5-invalidated.jpg) · [M15 исторически BUY](screenshots/09-m15-buy-historical.jpg)
- [Браузър 390 × 844](screenshots/10-m5-mobile-390x844.jpg) · [M5 Strategy Report](screenshots/12-audit-fixed-m5-report.jpg)

Кадрите за исторически преглед показват информацията към избраното затваряне. M5 SELL планът от 05:40 UTC показва вход 4058.790, SL 4066.491 и цели 4051.089, 4043.388 и 4035.687. Нивата съвпадат с `READY` записа. На мобилната ширина ценовата скала е компресирана ръчно, за да се виждат свещите и компактното табло. Strategy Report кадърът е от живата графика след контролния край на Pine Logs.

## Известия

Нативните Pine Logs потвърждават 81 събития в M5 и 53 в M15 сигнални payload-и. Нямаше S5 сигнали на M30. Отделното включване на EARLY WATCH генерира 72, 74 и 76 наблюдателни payload-а на M5, M15 и M30. Те носят стабилен идентификатор, UTC час и празни полета за вход и цели. Диапазонният филтър не отхвърли нито един от наблюдаваните READY планове. Нативните payload-и на одитната стратегия се сверяват с наблюдаваните симулирани сделки и насрочени стопове.

**Доставка `BLOCKED`.** В тестовия профил избрах `Any alert() function call` и `App`. При `Create` TradingView върна `Alert saving failed` и потвърди лимит **0 технически известия** за Basic. Push до iPhone остава непроверен. [Нативен отказ](screenshots/05-alert-blocked.jpg) · [Настройка за iPhone](README_BG.md#известия-на-iphone)

**Запазване `BLOCKED`.** `Save script` за новия RC2 скрипт отвори ограничението **1 запазен скрипт**, със заето място. Временният тестов раздел остана отворен в `PREMIUM`. Възстановяването след презареждане остава непроверено. [Нативен отказ](screenshots/13-save-blocked.jpg) · [Последен текущ изглед](screenshots/14-m5-current-premium.jpg)

## Възпроизвеждане

Експортирайте от Pine Logs при еднакви символ, тип свещи, рамка и заредена история. Изберете `Export RC2 QA journal` за RC2 и `Export RC1 QA journal` за контролите. Архивът съдържа оригиналните CSV файлове. От корена на хранилището изпълнете в PowerShell:

```powershell
python -B unified/tools/build_gold_pro_rc2.py --check
Expand-Archive -LiteralPath releases/gold-pro-rc2/evidence/native-logs.zip -DestinationPath extracted
python -B -m gold_pro.rc2_qa --native-dir extracted --output extracted/native-qa.json
python -B -m gold_pro.rc2_research --native-flat extracted --output extracted/research.json
python -B -m unittest discover -s tests
```

Нативният валидатор запазва `PARTIALLY_COMPLETE` при непокрити примери. За M30 липсват S5 входове и изходи. Извадката е историческа и включва вече разглеждани периоди. Отделният защитен forward период остава недокоснат.
