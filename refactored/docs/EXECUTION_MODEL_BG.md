# Модел на изпълнение и известни разлики

## Три отделни момента

1. WATCH: затворена свещ създава кандидат. Няма позиция.
2. CONFIRMED: по-късна затворена свещ изпълнява условията. Reference close, абсолютен SL и TP1/2/3 се замразяват с plan id. Няма твърдение за реален вход.
3. SIM_ENTRY / NATIVE_ENTRY: виртуално изпълнение от съответния модел. Реалната PU Prime MT5 цена не е известна.

При PAPER симулацията next-open се обработва при close на същия бар: `effective_time=open`, `known_time=close`. Това е хронологичен модел с отложено съобщаване, **не интрабар наблюдател**. За SL/TP touch между high/low няма точен timestamp; `effective_time=null`. Събитията съобщават кога информацията е станала известна.

## Custom OHLC симулатор

Входът използва първото налично open след сигналния бар. Проверяват се страната спрямо SL/TP1, отстоянието от reference, actual initial risk, cost/risk, room към предварително известната преграда и ръчно въведеният news window. При отказ има отделна cancelled заявка, не фиктивна губеща/печеливша позиция.

Абсолютните цели НЕ се преизчисляват от различния open. R се определя като `side * (fill_entry - initial_SL)` за първоначалната позиция. Всички частични резултати са претеглени спрямо целия първоначален риск.

SL-first: ако на една OHLC свещ има и SL, и поне една оставаща цел, всички оставащи части излизат по SL. Събитието е маркирано AMBIGUOUS; това е консервативно допускане, не знание за действителния път на цената. Предишни вече реализирани части не се отменят. При неблагоприятен gap през stop изходът е по open. При gap над цел лимитният изход е консервативно по фиксираната цел.

TP1 затваря 1/3; TP2 още 1/3; TP3 точния остатък. Повторен touch не отчита повторна печалба. След TP1 новият стоп се изчислява от виртуалния entry и първоначалния risk; влиза в сила от следващата свещ. Никога не се разширява назад. `profitLockR` е брутен буфер, не гарантирана печалба.

Timeout е заявка на close, изпълнение на следващо open. Ако преди timeout има SL или TP3, те приключват сделката. При следващо open през stop това се отчита като STOP_GAP, не като благоприятен close. Неизпълнени pending/активни позиции в края на данните НЕ се обявяват за приключили.

PAPER разходът е `spread_price + 2*slippage_ticks*tick + commission_price`; начислява се пропорционално на затворената част, общо веднъж за пълната позиция. Финансирането е цена/ден за посока, default0. При intrabar touch продължителността се оценява до close; това е явно допускане, не брокерски swap statement.

Дневният лимит е само на FILTERED, по отварянето на свещта в зададената IANA зона. Частичният net R участва веднага; дневната поредица е по окончателните нетни резултати. Нулев резултат не се брои за нова загуба. При достигане на лимит няма обещание за абсолютно ограничен live drawdown: има gaps и отворени части.

## Native Strategy Tester

Отделният файл действително декларира `strategy()`, подава market `strategy.entry` и три предварително поставени `strategy.exit` brackets с по една трета и отделно OCA име за всяка bracket двойка. Използва broker emulator на TradingView; `process_orders_on_close=false`, `calc_on_every_tick=false`, `calc_on_order_fills=false`, `use_bar_magnifier=true`.

Това НЕ означава, че Bar Magnifier е наличен за целия период/плана или че го тествахме. Списъкът с емулаторни изпълнения и Properties трябва да се запази при истинска проверка. Няма native forward test в този пакет.

**Неизбежни разлики спрямо custom PAPER:**

- Market order, подадена на предходния close, вече е изпълнена при next-open; кодът не може на следващия close честно да я отмени със задна дата. При нарушена PAPER open проверка се записва NATIVE_DIVERGENCE и по избор се подава exit за следващото open. Загубата не се изтрива.
- Native emulator сам избира изпълнения според данните и своите правила. Не е наложено custom stop-first за всяка свещ. Bar Magnifier може да даде различен резултат от OHLC допускането.
- Цената, частичните изходи, margin/capital и slippage могат да променят времето, когато общият портфейл е свободен. Затова се сравнява отделно потокът на техническите кандидати и потокът на изпълненията.
- Native profit lock се актуализира след действително отчетен exit с comment TP1, не просто след high/low touch. При затворена T1 част по SL тя не се подава повторно.
- Default native commission е 0.175 cash/unit **на страна**, slippage2ticks, initial capital100000 USD, 3 native units общо. Това НЕ са 3 MT5 лота. Комисионата означава 0.35 cash/unit roundtrip, еквивалент на 0.35 price само при pointvalue1. Провери Properties и `syminfo.pointvalue`; не приемай автоматично съответствие с PU Prime.
- Native `gross_r` тук използва реалните emulator entry/exit цени, които вече съдържат fill slippage; `cost_r` е native commission. PAPER `gross_r` е преди lump-sum cost, който включва и slippage. Не сравнявай gross колоните като идентични дефиниции.
- Native кодът не начислява отделен финансиращ cashflow; няма твърдение за swap parity.

Общото ядро на двата скрипта е byte-identical и има fingerprint в `reports/build.json`. Това доказва организацията на източника, **не** runtime parity на Pine, еднакъв broker fill или печеливш OOS резултат.

## Външни официални справки

Прегледани при реализацията; не валидират QIRVANTA:
- https://www.tradingview.com/pine-script-docs/concepts/strategies/
- https://www.tradingview.com/pine-script-docs/concepts/alerts/
- https://www.tradingview.com/pine-script-docs/concepts/repainting/
- https://www.tradingview.com/pine-script-docs/language/objects/
- https://www.tradingview.com/pine-script-docs/writing/limitations/
