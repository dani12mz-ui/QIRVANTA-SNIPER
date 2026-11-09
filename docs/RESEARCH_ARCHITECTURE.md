# Причинен Python research engine

Изпълнена е малка модулна библиотека, не външен AI service и не копие на Pine broker emulator. Тя не изпраща TradingView/MT5 поръчки и не променя baseline Pine логиката. Всички нови семейства са изключени по подразбиране. Статистическите изводи и ограниченията са отделни от техническата работоспособност.

## Данни и възпроизводимост

`research/data/loader.py` използва оригиналния `diagnostics/protocol/datasets/B-ALL-FULL/bars.csv`, без ново копие на raw данните. SHA-256: `7015f6386d2627237c57ff8625d59cbe3c9b852e4742b786cbb4679db3d48c56`. Това са 6 445 действително изнесени OANDA:XAUUSD M15 свещи от 02.08.2026 22:00 UTC до 09.11.2026 02:45 UTC, вече наблюдавани при предходното изследване. Сериализацията е до 8 десетични знака, не скритата точност на feed.

Няма untouched test в тези данни. Новото повторно наблюдение на платформата отчита 33 корекции на volume при непроменени OHLC/HTF; фиксираният research dataset остава оригиналният. Наличният volume не доказва действителен търгуван обем или VWAP. Няма PU Prime ticks, bid/ask, сделки или финансиране. M5/M30 се поддържат като входен формат и се проверяват синтетично; действителните допълнителни platform exports се приемат само след отделен QC. M15 не се раздробява в измислени по-малки свещи.

`research/data/aggregate.py::aggregate_m15_to_m30` допълнително обединява само две точно съседни M15 свещи в завършен UTC-aligned M30 bucket. O=първият O, H=max(H), L=min(L), C=последният C; volume се сумира, без да става доказан transaction volume. Непълните начални/крайни bins се пропускат **с пълен списък на source indices и причина**, а не се дописват. Manifest пази всеки използван чифт и хеш на получения масив; raw дубликат не се записва. Това е derived-M30 проверка на същата вече видяна история, не реален отделен M30 feed или untouched OOS. Числените параметри остават същите: 48 бара вече са 24 часа наблюдавани барове вместо 12, cooldown/feature периодите също се удвояват по clock time; next-open latency и двусмислеността са различни. Следователно ефектът не се приписва само на timeframe.

Действителният derived-M30 manifest съдържа **3 222** завършени бара и **1 пропуснат M15 бар** от непълен последен чифт. Общото начало е 02.08.2026 22:00 UTC; M30 завършва на 09.11.2026 **02:30 UTC**, докато M15 архивът завършва на 02:45. Това различие на последната граница остава видимо при сравнението.

Loader отказва null/nonfinite OHLC, невалидна ценова обвивка, дублирано/обратно/застъпващо време и смесена продължителност. Положителни OHLC и M5/M15/M30 са задължителни. Времевите пропуски остават в реда; няма имputation. Неизвестен/невалиден volume блокира само функциите, които реално зависят от него.

За допълнителни **действителни** platform exports има отделен `research/data/extract_platform.py`: разпознава qirvanta.native.v1 Pine Logs за M5/M15/M30, изисква очаквания strategy SHA и timeframe, проверява граници/count/snapshot, strict integer timestamps, duplicates/overlap и всички interval lengths. Записва CSV+manifest само в нова локална директория **извън repository**; отказва презапис. Това не разрешава публикуване на новите пълни OHLC данни без установени права. Наличието на extractor не доказва наличие или качество на конкретен export — новите действителни набори и евентуалните сравнения имат отделни QC manifests и предварителна регистрация. Оригиналният M15-only extractor в diagnostics остава непроменен.

Първият нов действителен M5 export е проверен: **5 565** свещи, 11.10.2026 22:00 UTC–09.11.2026 02:45 UTC, 20 запазени времеви прекъсвания. Локалният CSV SHA-256 е `44971f52b0008fefc4b61dda51c71043f08e4a2e3bc6d08121380e95232ace1d`; metadata-only manifest е `docs/platform_evidence/M5_dataset_manifest.json`. Native audit отделно потвърждава 46 завършени позиции/134 части и +50,438 USD с нулева accounting разлика. Този по-кратък период не се сравнява директно с целия Aug–Nov M15 baseline като доказателство за подобрение.

Новият действителен M30 export също е проверен: **10 091** свещи, 01.01.2026 23:00 UTC–09.11.2026 02:30 UTC, 220 времеви прекъсвания; CSV SHA `1b31dc5dad532b9362c5340511211fbc13a33c64983d4a49485c7442a269c5d5`. Native audit: 97 цели позиции/283 части, −376,557 USD, нулева accounting разлика и без отворени/липсващи позиции към cutoff. Metadata-only manifest е `docs/platform_evidence/M30_dataset_manifest.json`. Всичките 3 222 общи bins с derived-M30 имат еднакви OHLC; има 31 разлики във volume. Това е проверка на сериализираните цени, не signal/native parity. За сравнения на кандидатите върху реалните три timeframe exports има отделна регистрация с общ прозорец; различните пълни native периоди не се представят като пряко сравнима доходност.

Python 3.12.14 е провереният runtime. Числената библиотека е стандартната Python библиотека; float64 резултатите не се представят като bitwise Pine паритет. За IANA DST правилата се използва `zoneinfo`, проверено с `tzdata==2026.6` при празен системен TZPATH. Ако IANA базата липсва, S7 е BLOCKED, а session-filtered кандидати отказват; няма фиксирана UTC замяна, представена като DST.

## Feature слой

`research/features/causal.py::build_features(bars)` връща един dict за всеки затворен входен бар, с `index`, `time`, `available_at`. Няма смяна на предходни dicts при добавяне на бъдещи барове; prefix regression проверява това.

- EMA20/50/200: начално SMA от N наблюдения, после `EMA[t]=EMA[t−1]+2/(N+1)·(C−EMA[t−1])`. Това се различава от Pine EMA seed и периодите 21/55 на baseline.
- ATR14: TR=max(H−L,|H−Cprev|,|L−Cprev|), първи TR=H−L; seed SMA14, после Wilder α=1/14. ATR mean е последните 40 налични ATR.
- DM+/DM−: положителното по-голямо high-increase/low-decrease, равенството дава 0; Wilder14; DI=100·smoothedDM/ATR; DX=100·|DI+−DI−|/(DI++DI−), нулев знаменател→0; ADX=Wilder14(DX).
- RSI14: Wilder средни на положителните/отрицателните ΔC; `100·gain/(gain+loss)`, плоска серия→50. ER20 е нетното разстояние/изминатия път; плосък път→0.
- Range3/20 изключват текущия бар. Тяло, close-location, wick, FVG (`L[t]>H[t−2]` или обратното) са чисти OHLC описания, не наблюдение на институционален поток.
- Pivot 3/3 изисква строг high>всички 6 съседа или low<тях. Освобождава се на close на третия десен бар; равните extrema не са pivot. 7-баровият pivot прозорец не може да пресича времева дупка. Equal highs/lows: разлика между последните два потвърдени pivot≤0,12ATR.
- Предходни UTC day/week extrema са наличните наблюдавани барове от предходния период. Първият зареден ден/седмица може да е частичен; тези диагностични стойности **не управляват** кандидатните поръчки и не се представят като пълни provider session levels.
- H1/H4: агрегация в UTC часови граници; използва се само напълно затворен непрекъснат bucket, който започва на неговата граница. При непълен bucket bias=null. EMA20/50 върху пълни HTF closes; bias BUY ако close>EMA20>EMA50, SELL обратното, иначе 0. До 50 пълни HTF свещи bias е null. Това е изрично различен research context от baseline request.security.
- Session opening ranges: Europe/London 08:00–08:30 и America/New_York 09:30–10:00 местно време, делници. Всички барове на диапазона трябва да са налични. Диапазонът се освобождава след затваряне; DST е на всяка зона отделно, включително седмиците с несъвпадащи преходи.
- S6 VWAP изисква `volume_kind='EXCHANGE_TRADED'`, `verified_bar_vwap=True`, положителен finite volume и отделна проверена bar_vwap стойност в OHLC обвивката. `Σ(bar_vwap·volume)/Σvolume`, UTC дневно; σ е обемно претеглена дисперсия **на bar_vwap**, не недостъпна дисперсия на tick trades. Липсващ обем/бар нарушава дневната валидност. Самото high/low/close средно не се представя като истински transaction VWAP.

Regime при готовност: първо COMPRESSION ако ATR/mean<0,75; после EXPANSION ако >1,5; после UPTREND/DOWNTREND при подредени EMA, ADX≥20, ER≥0,25; RANGE при ADX<20 и ER<0,25; иначе TRANSITION. Преди готовност — UNSUPPORTED. Приоритетът е част от дефиницията; това е детерминистична хипотеза, не доказана класификация на скрит пазарен state.

## Решения и журнал

`research/strategies/library.py` съдържа отделните state machines и параметри. [Пълните математически правила](STRATEGY_INVENTORY.md) са спецификацията. Решенията съдържат family, side, signal ID, confirmation timestamp/index, raw trigger, status и всички проверени причини за отказ. NO_SETUP/WATCH не се наричат BUY fill. Disabled families се пазят в конфигурацията, не генерират изкуствени trades.

`research/backtesting/engine.py` изпълнява следващ-open модел, един портфейл, без pyramiding, fixed quantity и next-bar stop updates. Market/stop slippage и комисионата се начисляват точно веднъж. R targets са замразени projections от сигналния close; STRUCTURAL вариантът изисква действително известна цел ≥1R, иначе отказва.

Стабилна идентичност: signal ID=family|side|confirmation_time; plan ID добавя symbol, timeframe и configuration hash; entry ID и всеки exit component са под този plan. Всички partial exits се събират към същия план. Сигналите при зает портфейл, противоположните едновременни сигнали, повторните ID и cooldown се запазват като откази.

Полета за план: confirmation/entry/final-exit index и timestamp, reference, actual entry, initial/current stop, frozen targets и вид, quantity/remaining, първоначален cash risk, fees, gross/net cash, net R, TP flags, stop flag, breakeven flag, ambiguity, holding bars и exit reason. `exits` пази quantity, price, cash, fee и time precision за всяка част. `net_cash` и `fees` са резултатът и разходите на затворените части с пропорционалните входни такси. Отделно `fees_paid` и `cash_paid_net` включват **цялата входна комисиона още при entry**, включително за останалото quantity; `unallocated_entry_fee` е разликата между двете отчетни основи.

Схема v2: `equity_cash_paid` дебитира entry fee в момента на входа; при изход добавя gross P&L минус само exit fee. `all_realized_net_cash`/`cash_paid_net` са тази платена cash основа; `closed_parts_net` е предходната основа само на затворените части. Daily loss guard използва действителния симулиран cash дебит за съответния UTC ден, така че входна такса не се прехвърля към деня на изхода. Нито едната крива включва текущата нереализирана печалба/загуба.

Тази отчетна поправка е регистрирана **след** първите 90 икономически изпълнения, при независим review. Старите source/journal/report версии остават в `research/reports/method_history/paid_cash_precorrection/`. Поправката не променя сигналите, цените, количествата или пълния резултат на завършените позиции; default daily guard е OFF. Новият cash/DD timing и неплатеният в старата closed-part основа остатък от входните fees се показват изрично. Доказателството за повторението е отделен correction report; няма тихо заменена предходна версия.

**MAE/MFE са `mae_price_upper_bound` и `mfe_price_upper_bound`**, изчислени по пълните extrema на баровете, през които планът е бил активен. Последният бар може да има движение след изхода; тези полета не са точни intrabar excursion observations. На open exit целият бар пак дава само горна граница. Няма сумиране на partial MAE/MFE в измислена native equity крива.

COMPLETE е изцяло затворена първоначална позиция; OPEN и PENDING не влизат във full-plan win rate/PF. Няма принудителен final close. `closed_cash_drawdown` следва затворените части с разпределени fees; `cash_paid_drawdown` следва действителното време на cash плащанията. `native_equity_drawdown=None`, защото този engine не възпроизвежда native intrabar equity. Net R остава null при невалиден actual-entry risk; такава загуба остава в cash отчета.

## Използване

От repository root:

```python
from research.data.loader import load_bars
from research.backtesting.engine import BacktestConfig, run_backtest

bars = load_bars()  # проверява точния оригинален hash
disabled = BacktestConfig()  # всички експерименти OFF
config = BacktestConfig(enabled_families=('S1',), exit_variant='PARTIAL_R')
result = run_backtest(bars, config)
```

```text
python -B -m unittest discover -s research/tests -p "test_engine*.py" -v
python -B -m research.backtesting.engine --family S1 --output work/new-s1-run.json
```

CLI отказва съществуващ output, за да не замени стар резултат. `candidate_provider` е изричен **синтетичен test seam**; всеки такъв резултат съдържа `synthetic_candidate_provider=true`. Той не се използва в реалните изследователски runs. Външен provider не получава сертификат за причинност от engine.

За собствени експерименти се записва предварително ledger entry, версия на данните, пълна конфигурация, code hash и статут already observed. Текущият ledger е замразен преди историческото изпълнение на кандидатите. Самостоятелен кандидат и native baseline могат да се покажат на една таблица само с видимо разграничение на изпълнителните модели; разликата не се приписва автоматично на качеството на сигнала.

## Реално ограничен обхват

Реализирани са прости EMA/ATR/ADX/RSI/efficiency, структура, потвърдени pivots, сесии, H1/H4 агрегация, девет кандидатни правила и отказващ без доказателства селектор. Hurst/Kalman/CUSUM/HMM, wavelet/GARCH, order-flow/institutional liquidity, външни macro/news/consensus series и neural networks не са скрито „готови“. Няма подходящ point-in-time dataset или независимо положително доказателство за интеграцията им. Статистическият слой започва с интерпретируеми baselines и пази limitations отделно.

Нито Python inference, нито произволен ML модел могат автоматично да работят в Pine. Евентуално бъдещо внедряване би изисквало проверен преносим deterministic rule или отделна външна инфраструктура, нова независима проверка и отделно разрешение за реална търговия. Настоящата доставка не прави такава връзка.
