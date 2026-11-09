# QIRVANTA Обединен — инженерна проверка, 09.11.2026

**Основният файл за ежедневна употреба в този клон е [`unified/QIRVANTA_UNIFIED.pine`](unified/QIRVANTA_UNIFIED.pine).** Една Pine v6 strategy предоставя анализ, избран план, структурен SL, TP1–TP3 R проекции, информационни известия и Strategy Tester. Не се изисква втори индикатор.

- [Окончателен инженерен отчет](docs/FINAL_ENGINEERING_REPORT.md)
- [Реална TradingView проверка и ограничения](docs/TRADINGVIEW_ACCEPTANCE.md)
- [Всички сравнителни резултати, включително загубите](docs/EXPERIMENT_RESULTS.md)
- [Отстраняване на огромния Text drawing](docs/UI_DIAGNOSIS.md)
- [Режими на интерфейса](docs/UI_PINE_IMPLEMENTATION.md) · [Мобилни известия](docs/MOBILE_ALERTS.md)
- [Python research и възпроизвеждане](research/README.md) · [Покритие на заданието](docs/MISSION_COVERAGE.md)

Този етап е в `engineering/qirvanta-master-validation`, върху `dd1819515b6fe27e2a08bff91c1fbad6bfd7bcc9`. Champion baseline `48e5cddab17ea831bdc277f17f0406329fdfe39c` е запазен в историята и в оригиналния клон; сигналите и order логиката му не са сменени. [`refactored/`](refactored/), [`audited/`](audited/) и [`diagnostics/`](diagnostics/) остават непроменени. Генераторът е `unified/tools/build.py`; не сглобявайте тази версия от старите v7 компоненти. Main не се променя.

Новият интерфейс е компилиран в TradingView и възпроизвежда същите 72 позиции / 212 части / **−144.492 USD**, PF **0.905096** в фиксирания M15 период. **Няма доказано подобрение на доходността.** Новите офлайн кандидати остават OFF; S6/S10 са блокирани при липсващите им предпоставки. Реалните данни от новите exports остават локални; публичните derived журнали пазят всяка сделка и загуба, без цялата несигнална ценова серия.

Предишните [инструкции](unified/README_BG.md), [миграция](unified/MIGRATION_BG.md), [validation](unified/VALIDATION_BG.md) и [ACCEPTANCE.json](unified/reports/ACCEPTANCE.json) са исторически доказателства за записания там source hash. За текущия код използвайте новите `docs/` и платформени доказателства по-горе.

Симулацията не изпълнява реални MT5/брокерски поръчки. Обединяването не доказва по-добра доходност; обхватът на действителните compiler/runtime, parity, native accounting, alerts и mobile проверки се отчита отделно.
