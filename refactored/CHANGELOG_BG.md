# Промени спрямо последния audited baseline

- Възстановени src и детерминиран build от audited commit 103bc307, доказано точно сглобяване преди поправките.
- Build поддържа и двата delivery пътя и read-only byte-level `--check`; тестовете вече не презаписват output с по-стар код.
- Началният commit и точните Pine байтове са архивирани с SHA256; v7.6 не е използван за новото ядро.
- Неутрални посоки за WATCH/откази; пет основни реда с конкретни цени на вход/начален SL/текущ симулиран SL/TP1–TP3, R обозначения и отделна преграда; дата на последната затворена свещ; подробните исторически labels са по избор.
- Съкратени shorttitles; диагностичните native runtime съобщения съдържат конкретни символ/валути. След вход в профила е възпроизведен отказът за Premium-only High detalization; същият audited baseline заработва на H1 след Default. Native header вече задава `use_bar_magnifier=false`.
- Python reference съответства на вече съществуващите audited правила за open-targets/SL, замразен TP1 room, FILTERED context policy и native alert categories.
- Силна проверка на feed/timeframe/core/cost metadata, paired replay, UTF-8 и регресии срещу старите инструменти.
- Няма промяна на сигнални филтри, стоп/TP логика или разходи. Default вместо High променя native fill допусканията и е описан отделно; няма broker integration и няма push в main.
- И двата окончателни файла са компилирани и изпълнени на OANDA:XAUUSD Candles M5/M15/M30/H1/H4 без runtime отказ (smoke check). M15 и M30 отчитат загуби, запазени в отчета; различните периоди не се сравняват. При еднаква Default детайлност baseline/fixed показват еднакви видими native агрегати за 02.01–06.11.2026; пълна журнална parity и OOS не са доказани. Двете PRO CW10003 предупреждения са запазени и разгледани в отчета.
- Basic лимитът за 1 запазен личен скрипт е наблюдаван при Save; по избор на потребителя PRO е записан като личен `QIRVANTA_SNIPER_PRO`, версия 2. Старите chart instances са премахнати за спазване на лимита от 2 индикатора на графиката. Layout е записан и презареден: PRO остава видим, VALIDATION е скрит chart instance на M15. Няма закупен план.
- Действителните mobile viewport-и 390×844/430×932 показват застъпване на chart header от таблото; отчетът е `PARTIAL_FAIL_LAYOUT`, без твърдение за завършен mobile layout.
