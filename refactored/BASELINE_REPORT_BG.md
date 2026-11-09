# Начално състояние

Начален commit: `103bc30788b18234f5938ed0f5346e142097a6fe`; изтеглен е само `qirvanta-refactored-delivery` в нова директория. Началният git status е чист; не са презаписвани потребителски промени. Работен клон: `fix/audited-validation-integrity`.

Канонична начална версия: `audited/QIRVANTA_SNIPER_PRO.pine` (1234 реда) и `audited/QIRVANTA_SNIPER_VALIDATION.pine` (1400 реда). Началните Git байтове и целият commit са запазени в `baseline/audited/`, `baseline/delivery_103bc307.zip`; всички SHA256 са в `baseline/delivery_manifest.json`. Отделно `reports/source_recovery.json` съдържа хешовете след единствено нормализиране на CRLF към LF:

| Файл | SHA256 LF |
|---|---|
| PRO | ee0a692777d439e9957957f6abb241420a0478578e10c7b906ab1d1a651ed30a |
| VALIDATION | 5cd7a04a6d97dfc1c37024f0d004874553c163a3b17b9984c03336f3aa9004a3 |

В началото `refactored/src`, генераторът и тестовете са по-стари: core `118d431271946bfb66e199ad9f795b0fe588a40817a5a11ec7fb69975c162b3d` вместо audited `c189ef479ccc106178a8a3b146da0dcd7d2fac852ddbeb8eebb298f385748dcc`. Публикацията в audited изрично не включва актуалните компоненти. Старият build е изпълняван само в изолирано копие на началния архив за възпроизвеждане на тестовете.

Налични: Git 2.56.0.windows.4, bundled Python, стандартна библиотека и tzdata, GitHub read connector, TradingView chart/Pine Editor в in-app browser. `python` в PATH сочеше неработещ Windows Store alias; използван е реалният bundled python.exe. Версията и командите са записани в `reports/baseline_commands.json` и текущия tests.json.

Начални проверки: без UTF-8 режим 62 tests, 1 setup error (cp1251). С `-X utf8`: 85 tests, 1 failure (старият baseline SHA assertion не отчита CRLF). Логовете са `baseline_tests_0.log`, `baseline_tests_1.log`; те проверяват стария refactored, **не последния audited**. Архивното твърдение за 151 теста не е използвано като доказателство.

TradingView: OANDA:XAUUSD, 15m, Candles, Add to chart → Log in. Не е достигнат compiler result. Windows Firefox опитът преди това завърши с app approval timeout. Няма налични реални експортирани OHLC или Pine журнали; само синтетични fixtures. Старите числа от снимки не са текущ резултат за PRO.
