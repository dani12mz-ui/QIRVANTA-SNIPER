# Риск и изпълнение

Няма връзка за изпращане на поръчки, автоматична търговия или разрешение за live изпълнение. `research/risk/calculator.py` е отделен офлайн калкулатор. Реалният профил `broker_profile_unverified.json` остава **UNVERIFIED_BLOCKED**: не са известни account/server спецификациите за PU Prime `XAUUSD.s`, таксите, conversion rate, текущите bid/ask и margin. Синтетичната тестова спецификация не е брокерна препоръка.

## Какво е проверено в официалните източници

PU Prime публикува общ пример за минимален gold обем 0.01 lot. Това не удостоверява точното `XAUUSD.s` на конкретния сървър, стъпката, максимума, стоп ограниченията и таксите. [PU Prime Help Center](https://helpcenter.puprime.com/hc/en-001/articles/360004250936-What-is-the-minimum-lot-size-that-I-can-trade), проверено 09.11.2026.

MT5 позволява преглед на конкретния инструмент чрез Market Watch → Specification. Изискват се contract size, point, tick size, tick value за загуба, volume min/max/step, stops/freeze level и валута. Минималният lot не доказва lot step; displayed point не доказва tick size. [MT5 Specification](https://www.metatrader5.com/en/terminal/help/trading/market_watch), [MQL5 Symbol Properties](https://www.mql5.com/en/docs/constants/environment_state/marketinfoconstants), проверени 09.11.2026.

`OrderCalcProfit` оценява резултат при зададени цени и обем във валутата на сметката. Това е правилна независима проверка за конкретния account calculation mode; настоящият калкулатор поддържа само верифициран линейно зависим CFD и не извиква MT5. [Официална функция](https://www.mql5.com/en/docs/trading/ordercalcprofit).

TradingView различава chart close от изпълнение по bid/ask. Това предупреждение не доказва универсален тип цена на всеки feed. Нашата OANDA OHLC история няма PU Prime bid/ask и не установява, че визуален touch на TP/SL е изпълним при брокера. [TradingView: execution price versus chart close](https://www.tradingview.com/support/solutions/43000647247-why-is-the-price-of-the-order-execution-different-from-the-close-price-on-the-chart/).

## Офлайн смятане с отказ при неизвестни входове

След проверен account/server snapshot, със срок на валидност, BUY използва ask за вход и bid за минималното разстояние до stop; SELL използва bid за вход и ask за stop. Stop трябва да лежи на реалния tick grid. Калкулаторът взема по-строгото от stops/freeze ограниченията и спира при stale/future quote/spec, липсваща валутна проверка, news pause, дневен/портфейлен лимит и невалидна геометрия.

Оценката за загуба на lot е `(entry−stop)×side / tick_size × tick_value_loss` плюс явно зададени adverse entry/exit ticks и round-trip commission. Spread вече участва чрез изпълнимия вход; не се добавя втори път. Cash budget е минимумът от риска за позиция, оставащия общ риск и остатъка до дневния лимит. Обемът се закръгля надолу към **цяло кратно на step от нула**, после се проверява срещу minimum/maximum. Не се използва `minimum+n×step`; независимият review откри и поправи този случай с тест за minimum 0.03 и step 0.02. Ако допустимият минимум надвишава бюджета, резултатът е BLOCKED. [Официално изискване за кратност на volume step](https://www.mql5.com/en/book/automation/symbols/symbols_volume).

Това не гарантира максимална загуба: gap, ликвидност, slippage, промяна на conversion и overnight financing могат да я превишат. Не са проверени margin/filling permissions или действителни broker fills. Настройките 0.5% за позиция, 1% общ риск и 2% дневен лимит в функцията са изрични примерни предпазни параметри на тестовия калкулатор, не оценка на подходящ риск за потребителя.

## Разходи и журнал

Базовият TradingView журнал е с quantity 3, pointvalue 1, commission 0.175 на unit на fill и slippage 2 ticks при mintick 0.001. Това са условия на симулацията, **не 3 PU Prime lots**. Цените на native fills вече съдържат симулирания slippage. Комисионата се изважда веднъж; `NATIVE_EXIT` partial и обобщен `NATIVE_CLOSED` не се събират като две печалби.

Native RAW не наследява автоматично paper/shadow statistical или daily-R guards. Същите не трябва да се рекламират като защита на фиксирания native размер. Python `daily_loss_limit_cash` и `max_risk_cash` са `None` по подразбиране; те не са включени в описаните сравнителни симулации.

Допълнителният native stress задържа действителните fills и добавя само декларирана наказателна разлика: 1.5× fees/+1 tick на fill дава −182.724 USD; 2× fees/+2 ticks дава −220.956 USD. Това не е backtest върху реални исторически спредове. Python candidate cost variants преизпълняват собствената консервативна OHLC логика при 1.5×/2× commission и slippage; engine parity с native не е доказана.

В Python v2 вече има два ясно отделени ledger-а: `equity_closed_cash` приписва entry fee на затворената част за сравнимост, а `equity_cash_paid` дебитира пълната entry fee на действителния вход и само exit fee при изход. Daily cash guard използва втория. `all_realized_net_cash` е paid-cash, включително оставащото отворено количество; completed campaign net пази еднократното начисляване на всички такси. Поправката е регистрирана и 90 еднакви сценария са преизпълнени с непроменени сигнали/fills. S7 BASE остава с 0.175 USD повече платен разход спрямо стария allocated ledger, който вече не се представя като наличен cash.

Реалната нова TradingView репродукция на същия стар cutoff връща същите 72 кампании, но 33 volume клетки са коригирани от доставчика. Използваната research версия е заключена по SHA256. VWAP/volume модели остават блокирани; не се приема централизирано реално изтъргувано количество за този OTC feed.

## Неосигурени данни

Няма point-in-time новини/макро календар с наличност в момента на решението, DXY/yield revision history, PU Prime bid/ask/ticks, broker execution export или бъдещ holdout. По-късно в мисията са извлечени действителни M5 и M30 OANDA истории и native журнали; общият research прозорец е 23.10.2026 20:00–09.11.2026 02:30 UTC след еднакво правило за наличен H1/H4 warmup. Новата granularity не е недокоснат календарен holdout. Raw OHLC остава локално, без нова публикация на неустановени права за данни. News pause остава ръчен. Не е създаден hindsight macro filter. Всички нови research families/модели са OFF по подразбиране; champion не се сменя от положителна историческа таблица.
