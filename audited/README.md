# Текущи QIRVANTA файлове

Тази папка се генерира от възстановените `../refactored/src/` компоненти на последния audited baseline103bc307. Текущият code commit е51e0aa2.

- [PRO](QIRVANTA_SNIPER_PRO.pine) — графика и информационни известия.
- [VALIDATION](QIRVANTA_SNIPER_VALIDATION.pine) — Strategy Tester.
- [Инструкции](../refactored/README_BG.md), [проверки](../refactored/VALIDATION_BG.md), [acceptance](reports/ACCEPTANCE.json).

И двата окончателни файла са компилирани и изпълнени на OANDA:XAUUSD M5/M15/M30/H1/H4 като проверки за зареждане и изчисляване. PRO има две разгледани предупреждения CW10003. Наблюдаваният runtime отказ на Basic е възпроизведен при High detalization/Premium; текущият VALIDATION използва Default чрез `use_bar_magnifier=false`.

Мобилните390×844/430×932 проверки показват застъпване на таблицата с header и нямат PASS. Няма пълно изпълнително съгласуване, OOS/forward или доказана доходност. Старите login blocker отчети са исторически; текущият е `reports/ACCEPTANCE.json`.

Публикация в GitHub: PENDING към подготовката на този отчет. Клонът е `fix/audited-validation-integrity`; main не се променя.
