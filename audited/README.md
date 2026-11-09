# Текущи QIRVANTA файлове

Тази папка се генерира от възстановените `../refactored/src/` компоненти на последния audited baseline 103bc307. Тестваният code commit е 51e0aa2. Кодът е публикуван в [отделния клон](https://github.com/dani12mz-ui/QIRVANTA-SNIPER/tree/fix/audited-validation-integrity), [потвърден commit](https://github.com/dani12mz-ui/QIRVANTA-SNIPER/commit/3495660fe88200b6d8c8ebd5c0a7f179da0d8eed) `3495660fe88200b6d8c8ebd5c0a7f179da0d8eed`. Main и началният baseline са непроменени.

- [PRO](QIRVANTA_SNIPER_PRO.pine) — графика и информационни известия.
- [VALIDATION](QIRVANTA_SNIPER_VALIDATION.pine) — Strategy Tester.
- [Инструкции](../refactored/README_BG.md), [проверки](../refactored/VALIDATION_BG.md), [acceptance](reports/ACCEPTANCE.json), [публикационни данни](reports/publication.json).

И двата окончателни файла са компилирани и изпълнени на OANDA:XAUUSD M5/M15/M30/H1/H4 като проверки за зареждане и изчисляване. PRO има две разгледани предупреждения CW10003. Наблюдаваният runtime отказ на Basic е възпроизведен при High detalization/Premium; текущият VALIDATION използва Default чрез `use_bar_magnifier=false`.

Запазеният личен PRO, версия 2, е отворен повторно и целият текст съвпада с финалния файл след LF нормализиране. След reload на запазения layout PRO е видим, а VALIDATION е скрит chart instance; включва се с окото/Show до QIRV TEST.

Мобилните проверки 390×844/430×932 показват застъпване на таблицата с header и нямат PASS. Няма пълно изпълнително съгласуване, OOS/forward или доказана доходност. Старите login blocker отчети са исторически; текущият е `reports/ACCEPTANCE.json`. Отчетите и последното TradingView доказателство следват потвърдената кодова публикация в документационен commit.
