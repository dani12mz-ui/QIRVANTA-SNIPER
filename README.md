# QIRVANTA Обединен — един файл за TradingView

**Основният файл за ежедневна употреба в този клон е [`unified/QIRVANTA_UNIFIED.pine`](unified/QIRVANTA_UNIFIED.pine).** Една Pine v6 strategy предоставя анализ, избран план, структурен SL, TP1–TP3 R проекции, информационни известия и Strategy Tester. Не се изисква втори индикатор.

- [Инструкции](unified/README_BG.md)
- [Миграция, настройки и разлики в изпълнението](unified/MIGRATION_BG.md)
- [Проверки и ограничения](unified/VALIDATION_BG.md)
- [Действителен статус и доказателства](unified/reports/ACCEPTANCE.json)

Клонът `feature/qirvanta-unified` започва от поправената локална доставка. [`refactored/`](refactored/) и [`audited/`](audited/) остават неизменен baseline с предишните PRO/VALIDATION и техните тестове; v7.x файловете са исторически архив. Текущият генератор е `unified/tools/build.py` и записва само един общ файл. Main и старите delivery клонове не се променят.

Симулацията не изпълнява реални MT5/брокерски поръчки. Обединяването не доказва по-добра доходност; обхватът на действителните compiler/runtime, parity, native accounting, alerts и mobile проверки се отчита отделно.
