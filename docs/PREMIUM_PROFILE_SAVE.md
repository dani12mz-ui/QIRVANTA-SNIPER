# Premium · TradingView профил

**`PROFILE_SAVE_BLOCKED` · `NOT_SAVED`**. В поддържания вграден браузър се зареди вписаният TradingView профил без повторен вход. `Unnamed` беше OANDA:XAUUSD M5, `All changes saved` и **Autosave ON**. `Manage layouts → Make a copy…` отвори съобщение, че Basic има лимит **1 saved layout**. Не е прекъсван Firefox; не са сменяни профили, настройки за сигурност или глобална тема. Не е изключван Autosave, не са премахвани скриптове и няма запис върху оригинала. Няма нов Premium layout, запис или reload доказателство. [Как TradingView синхронизира layout-и](https://www.tradingview.com/support/solutions/43000692404-layouts-charts-drawings-indicators-and-their-interaction/).

Инсталиране при наличен отделен layout:

1. Направи копие в `Manage layouts → Make a copy…` и го именувай `QIRVANTA SNIPER PREMIUM — XAUUSD`. Провери, че това е копието, преди да редактираш графиката.
2. Pine Editor → `Create new` → `Strategy`. Постави целия `unified/experimental/QIRVANTA_SNIPER_PREMIUM.pine` и използвай `Add to chart`. Първо провери действителното съобщение за компилация и runtime; draft не е запазен в личната библиотека.
3. OANDA:XAUUSD, обикновени свещи, M5. В `Стратегия за независим тест` избери **S5** (фабричният S0 остава контрол). `RAW`, `Всички`, `АВТОМАТИЧЕН`, `Изследване на вход и изход=OFF`, `1` единица във всяка част, 1R/2R/3R, буфер 0,05R след TP1; допълнителни H1, FVG, PD, Room и сесиен филтър OFF. Properties: 100 000 USD, 3 units, 0,175 USD/unit/fill, 2 ticks, pyramiding 0, следващ тик, без Bar Magnifier.
4. `PREMIUM`, долу вляво, 11 pt, EMA OFF, BUY/SELL и 10 исторически надписа ON, Entry/начален/текущ SL/TP1–TP3 ON, H1/H4 ON, `Risk / Reward zones=ON`, прозрачност 91/94, `Closure`, история 1. В Style остави двете Pine серии за заявки и `Trades on chart` видими. Общият ключ за известия остава OFF.
5. Свери архивни BUY и SELL на **свещта на заявката** и отделно native fills, активна геометрия, closure и desktop/мобилен изглед. Едва тогава запази копието и презареди. Провери името, M5, S5, PREMIUM, Style и непокътнатия оригинал.
