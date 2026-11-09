# Проверки и оставащи ограничения

Статусът по категории и точният тестван commit/файлов fingerprint са в `reports/ACCEPTANCE.json`. `CODE_TESTS` означава Python поведение, проверка на артефакти и статични Pine контракти; не Pine runtime.

## Защо VALIDATION не се добавя

Първият опит с началния audited VALIDATION стигна до Add to chart и Log in. След вход в разрешения профил отказът беше възпроизведен на OANDA:XAUUSD, H1, стандартни Candles: „Upgrade to the Premium plan to access higher bar detalization with more ticks used in calculations.“ Менюто показваше High detalization (~24 ticks/bar). Същият код заработи след превключване към Default detalization (4 ticks/bar), без промяна на сигналните настройки. Копираният от Pine Editor код съвпада с началния audited VALIDATION след LF нормализация: SHA256 `5cd7a04a6d97dfc1c37024f0d004874553c163a3b17b9984c03336f3aa9004a3`.

Проверени в кода пътища за runtime.error: нестандартни свещи; рамка извън M5/M15/M30/H1/H4; HTF не е по-висока; обърнат TP/SL/ATR диапазон или период; невалиден pointvalue; различна валута на символа и Strategy account currency. Последните два са само в VALIDATION. Съобщенията им вече съдържат `NATIVE_POINTVALUE`/`NATIVE_CURRENCY`, символа, валутите и действието. За XAUUSD/USD няма доказателство, че тези guards са били причината.

От юли 2026 Bar detalization заменя стария UI checkbox за Bar Magnifier. `use_bar_magnifier=true` избира High и изисква Premium/Ultimate; това съответства на наблюдаваната runtime грешка. Поправката е `use_bar_magnifier=false` в native header, със запазени всички други execution defaults, сигнални филтри и разходи. `calc_on_every_history_tick` е отделна настройка и остава изключена. Shorttitle е съкратен до QIRV PRO / QIRV TEST за премахване на предупреждение, не е причината за runtime отказа. [Декларации](https://www.tradingview.com/pine-script-docs/language/declaration-statements/#use_bar_magnifier), [release notes](https://www.tradingview.com/pine-script-docs/release-notes/), [детайлност и изпълнение](https://www.tradingview.com/support/solutions/43000786180-detalization-and-execution/).

## Наблюдаван резултат и отделни окончателни проверки

Началният baseline след Default и окончателният VALIDATION с Default по подразбиране показват еднакви native UI обобщения на OANDA:XAUUSD H1 Candles за видимия период 02.01–06.11.2026: 110 сделки, 51 положителни/59 отрицателни/0 нулеви, PnL +667.34 USD, PF 1.248 и max drawdown 649.13 USD. Native разходите в headers са еднакви; inputs не са променяни ръчно. Снимки: `reports/tradingview_evidence/tradingview_baseline_standard.png` и `reports/tradingview_evidence/tradingview_fixed_standard.png`. Това е ограничено сравнение на видимите агрегати при един provider/период/режим; без пълен OHLC/журнал export не доказва изпълнение по изпълнение parity, OOS, forward резултат или подобрена доходност.

Всеки окончателен файл изисква собствен Save / Add to chart резултат с пълни compiler errors/warnings и SHA. Матрицата е OANDA:XAUUSD, стандартни свещи M5/M15/M30/H1/H4, записани период, loaded bars и настройки. В Properties: On every tick OFF, On order fills OFF, Fill orders on close OFF, On history bar tick OFF (ако е налично), Default detalization. За съпоставяне baseline/fixed използвай еднакъв режим, а не High срещу Default.

След baseline теста окончателният VALIDATION (SHA256 `44a579eee6e1542fdb7f30b9dceb9ec78aac9f3c6d9bc0346a52fa4d9bf529b2`) има наблюдавани „Compiled“ / Add to chart в 23:12:36 местно време. PRO има успешни compile logs в 23:14:29 и 23:16:55 и е записан като личен `QIRVANTA_SNIPER_PRO`, версия 2. След премахване на старите chart instances, за да се спази Basic лимитът от 2 индикатора на графиката, двата окончателни файла са изпълнени едновременно на H1 без наблюдаван runtime отказ. Тези факти са записани в `reports/tradingview_attempt.json`; H1 runtime е отделно наблюдение след компилацията, не извод само от compile logs.

PRO предупреждения: CW10003 при `f_fast` (744:35) и `f_reversal` (745:35) за условното извикване на функциите. Прегледът установи, че използваните исторически серии са глобални, без възпроизведен поведенчески дефект от тези предупреждения; това не доказва историческа/realtime parity. Предупрежденията са запазени в отчета, не са скрити чрез произволна промяна на сигналната логика.

Двата окончателни файла са заредени и изчисляват без наблюдаван runtime отказ на M5/M15/M30/H1/H4. Това е `PASS_RUNTIME_SMOKE`, а не replay, историческа каузалност, realtime rollback или forward проверка. Loaded-bar броят не е записан. Native UI наблюденията са:

| Рамка | Видим период | Сделки | Печеливши/губещи | PnL USD | PF | Max DD USD |
|---|---|---:|---:|---:|---:|---:|
| M5 | 2026-10-12 – 2026-11-06 | 134 | 66/68 | +50.44 | 1.100 | 217.51 |
| M15 | 2026-08-03 – 2026-11-06 | 212 | 90/122 | -144.49 | 0.905 | 842.26 |
| M30 | 2026-01-02 – 2026-11-06 | 283 | 123/160 | -376.56 | 0.904 | 1595.16 |
| H1 | 2026-01-02 – 2026-11-06 | 110 | 51/59 | +667.34 | 1.248 | 649.13 |
| H4 | 2023-01-03 – 2026-11-06 | 123 | 74/49 | +2284.47 | 2.055 | 892.79 |

Всички рамки показват 0 breakeven сделки. Периодите са различни и резултатите между рамките не се сравняват; загубите на M15 и M30 са запазени. Baseline/fixed сравнението е само H1. Суровите UI snapshots и снимки са в `reports/tradingview_evidence/`.

В Properties са прочетени: 100000 USD capital, quantity 3, pyramiding 0, commission 0.175 Per contract, slippage 2 ticks, leverage 1x/1x, Default (4 ticks per bar), On bar close, One tick order delay и Requested price limit execution. Inputs не са променяни ръчно. Точните изпълнени кодови артефакти са от commit `51e0aa20a5604b9d79e7214f6e2aca59ac01048a`, source fingerprint `c7946faaa06cfc85ae1a757a5a9804fdffeb6552be9112dec20a9dc5ae674cb3`.

Генерираният коментар `PINE_COMPILE_UNVERIFIED` е build-time banner. Текущият датиран отчет удостоверява последващите реални compile/runtime наблюдения за посочените SHA, без промяна на Pine байтовете заради коментара. Delivery на alerts, пълни native trade logs и реален replay не са удостоверени. Действителни viewport проверки 390×844 и 430×932 показват застъпване на chart header от таблото горе вдясно: `MOBILE_UI: PARTIAL_FAIL_LAYOUT`. Снимките са `reports/tradingview_evidence/tradingview_mobile_390.png` и `tradingview_mobile_430.png`. Преместване долу вдясно е възможна display настройка, но не е удостоверена като поправка. Няма Pine CLI заместител.

Записването срещна отделно ограничение: TradingView показва Basic план с максимум 1 запазен личен скрипт. По избор на потребителя PRO е записан като версия 2 на личния `QIRVANTA_SNIPER_PRO`; VALIDATION не е записан като втори личен скрипт. Save/reload на layout потвърди PRO видим и VALIDATION (QIRV TEST) скрит на M15; стратегията се запазва като chart instance. Окото/Show до QIRV TEST я показва със Strategy Report. Отделният Pine файл също се предава. Не е купуван план и не се заобикалят ограниченията на платформата.

## Сравнение

Сигналното/PAPER ядро на baseline audited и поправената версия е байтово еднакво след LF нормализация. Native orders, разходи и сигнални филтри са запазени. Единствената нова native execution настройка е Default вместо High detalization; тя променя модела на intrabar fills. Идентичният core не доказва еднакви fills между тези режими. За честно baseline/fixed сравнение и двата трябва да са Default със същите данни и разходи.

`reports/synthetic_tooling_comparison.json` сравнява стария Python инструмент с поправения по еднакви измислени OHLC, tick и разходи. Той документира поправка на инструмента, не подобрение на Pine стратегията. При неизвестен SL/TP ред загубите остават; RAW и независимите моделни книги продължават да отчитат блокираните от FILTERED случаи.

За действително сравнение експортирай пълни baseline/fixed Pine JSONL събития върху един и същ CSV и настройки. Използвай `tools/compare_runs.py` (README има точната команда). Различни provider/symbol/timeframe/config/pine_inputs се отказват; core hashes може да са различни и се записват. Журналите се пазят по plan_id, не само общ сбор. Няма независимо възпроизвеждане на Pine сигналните условия в Python.

CSV: `time,time_close,open,high,low,close`; времена UTC Unix milliseconds или ISO с timezone; без припокриващи се свещи. JSONL: пълният `qirvanta.event.v1` export, включително core_sha256, symbol, timeframe, замразени нива/разходи и context fields. `data/MANIFEST_EXAMPLE.json` е шаблон, не данни; попълни реалните настройки и coverage evidence. Валидаторът проверява съгласуваност, не удостоверява независимо произхода/пълнотата.

Native fills и custom PAPER не са едни и същи: native не отменя изпълнен next-open гап; при SL/TP в една свещ emulator може да избере друг ред. 0.175 cash/contract/side не е универсално еквивалентно на PAPER spread 0.35 price; зависи от pointvalue. За справедливо сравнение baseline/fixed в един engine използвай идентични разходи. Native/PAPER parity изисква отделно съгласуване по символ и журнал. Не сравнявай TradingView feed с MT5 като еднакви данни.

OOS/forward performance: `PERFORMANCE_UNVERIFIED_MISSING_DATA`; няма оптимизация, train/test tuning или твърдение за положителна доходност.
