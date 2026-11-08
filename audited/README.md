# QIRVANTA PRO — последни готови Pine файлове

Тази папка съдържа готовите файлове от **QIRVANTA_PRO_CURRENT.zip**. Те заменят предишните файлове на същите адреси; историята им остава в Git. Клонът `main` не е променен.

## За копиране

- **[Индикатор PRO](QIRVANTA_SNIPER_PRO.pine)** — анализ и предупреждения върху графиката.
- **[Тестова стратегия VALIDATION](QIRVANTA_SNIPER_VALIDATION.pine)** — отделен скрипт за Strategy Tester.
- **[PRO — само код / Raw](https://raw.githubusercontent.com/dani12mz-ui/QIRVANTA-SNIPER/qirvanta-refactored-delivery/audited/QIRVANTA_SNIPER_PRO.pine)**
- **[VALIDATION — само код / Raw](https://raw.githubusercontent.com/dani12mz-ui/QIRVANTA-SNIPER/qirvanta-refactored-delivery/audited/QIRVANTA_SNIPER_VALIDATION.pine)**

Отвори Raw, копирай целия код и го постави в отделен Pine скрипт. Не обединявай двата файла. След смяна на скрипта пресъздай съответните TradingView известия.

## Каква е доставката

Готовите Pine файлове са качени без промяна в съдържанието. Git blob хешовете им съвпадат с локалните файлове от последния пакет. В `SOURCE_FILES.json` има размери и SHA-256 хешове.

Това е **публикация на самостоятелните готови скриптове и придружаващата документация**, не огледало на целия ZIP. `src/`, `tools/`, `tests/`, baseline архивите, PNG макетите и самият ZIP от последния пакет не са качени с тази публикация. По-старата папка `refactored/` НЕ е източникът за повторно генериране на тези текущи файлове. Командите за build/test в оригиналното `README_BG.md` се отнасят за пълния локален пакет.

## Проверки и ограничения

[Инструкции](README_BG.md) · [Текущ одит](AUDIT_BG.md) · [Acceptance](reports/ACCEPTANCE.json) · [Build хешове](reports/build.json) · [Резюме на локалните тестове](reports/tests.json).

Отчетите са запазени от подготовката на последния пакет. При тази GitHub публикация са проверени прехвърлянето и файловите хешове; не са изпълнявани нови тестове. Полетата `GITHUB.modified=false` и `GITHUB.published=false` в архивния acceptance отчет описват момента на локалното му създаване, преди настоящото качване.

**Pine компилацията, runtime в TradingView, независимата доходност и forward тестът остават непотвърдени.** Локалните 151 проверки не са Pine компилация или доказателство за печалба. Инструментът не знае и не управлява реалните позиции в MT5.
