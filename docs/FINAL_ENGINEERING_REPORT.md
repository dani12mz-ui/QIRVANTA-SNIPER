# QIRVANTA UNIFIED — окончателен инженерен отчет

**Поправени са интерфейсът и установени дефекти в изследователската отчетност. Подобрена доходност не е доказана. Champion сигналите остават непроменени; нито един нов кандидат не е включен в Pine.**

Работният клон е `engineering/qirvanta-master-validation`, върху `dd1819515b6fe27e2a08bff91c1fbad6bfd7bcc9`. Baseline `48e5cddab17ea831bdc277f17f0406329fdfe39c` остава в историята и оригиналния клон. `main`, `audited/`, `refactored/` и предишните `diagnostics/` не се редактират. Описът преди работа е [MISSION_BASELINE.json](MISSION_BASELINE.json).

## A. COMPLETED — действителни промени

- Установен е отделен TradingView **Text drawing**, съдържащ 115 724 знака от предишния диагностичен редакторен буфер. Скрит е обратимо през Object Tree. Това не е Pine label; няма доказателство кой го е поставил. [Диагноза и инструкции за Hide/Remove](UI_DIAGNOSIS.md).
- Един production [QIRVANTA_UNIFIED.pine](../unified/QIRVANTA_UNIFIED.pine), Pine v6, SHA256 `92307768be68946dc87cbe4ea45a8b5e32446fd3b7a0c1a38dce40dffe358be3`. MINIMAL/STANDARD/PRO/DEBUG, отделни визуални контроли, ограничени коментари, повторно използвани обекти и компактно българско табло. Entry/структурен SL/R проекции/структурна преграда са различени. Статусът на подготовка не се нарича покупка или изпълнена позиция. [Точен обхват](UI_PINE_IMPLEMENTATION.md).
- Информационен H1/H4 контекст е отделен от търговските решения и използва затворени стойности. Няма нов сигнален филтър или скрито преместване на входове.
- Модулен Python research: причинни regime/structure/momentum/session features, completed HTF aggregation, кандидати S1–S9 и fail-closed S10 selector, консервативен OHLC simulator, plan/entry/exit IDs, частични изходи, касови криви, offline risk calculator, triple-barrier helper, logistic/calibration baseline, purge/embargo, walk-forward и uncertainty анализ. Всички кандидати са OFF по подразбиране. S6 е блокиран без надежден VWAP/volume; S10 — без независимо валидиран routing manifest. [Архитектура](RESEARCH_ARCHITECTURE.md), [точни правила](STRATEGY_INVENTORY.md).
- Предварително регистрирани изследвания: 80 семейство/exit/cost/delay опита; 10 derived-M30; 30 actual M5/M15/M30 в общ прозорец; 9 отделни parameter/gate проверки. Всички резултати, нулеви и блокирани случаи са записани. Accounting rerun не се брои като нова хипотеза.
- Поправени са entry-fee timing в Python, проверката за дробни timestamps, неправилно дегенериращ CI при много малък sample и lot-step rounding в offline калкулатора. Старите резултати/source snapshots са запазени; поправките са регистрирани и сравнени. Нито една загуба не е изтрита.

## B. VERIFIED — какво доказват проверките

**Реален TradingView:** компилация и изпълнение на M5/M15/M30/H1/H4; source readback след възстановяване на чистия production; 118/118 текущи Inputs и 11 Properties; всички четири UI режима и mobile viewport 390×844. [Платформен отчет и снимки](TRADINGVIEW_ACCEPTANCE.md).

**Реална M15 parity:** непромененият baseline и новият UI са изпълнени върху точно еднакви 6 445 повторно наблюдавани бара. Всички 72 позиции/212 части са идентични; изключено е само името на run, без изключени финансови полета. Спрямо стария архив има 33 provider volume ревизии, които са изрично документирани. [Machine-readable доказателство](platform_evidence/ui_native_parity.json).

**Локални регресии: 213 различни test methods, всички PASS.** 109 unified — [log](../unified/reports/master_ui_tests.log), [manifest](../unified/reports/master_ui_tests.json); 104 research в един общ процес (62 feature/strategy/execution/data/ablation, 36 statistics/model/risk/validation, 6 publication) — [общ log](research_integration_tests.log), [окончателен TEST_SUMMARY](TEST_SUMMARY.json). Общият research run проверява и липсата на замърсяване на състоянието между компонентите. Focused reruns не увеличават броя; 129 scenario replays са отделно доказателство. Unit тестовете не се представят като Pine компилатор или физически phone тест.

Защитената проекция на trading source е byte-identical, SHA256 `114df1fddfcc1282c3fe3aff430fc306ba0e215787fb05a2845be04cabdea95d`. Съответствието на source и генератора е проверено. Независимият replay проверява causal prefixes, BUY/SELL симетрия, paid fees, partial/open quantities и запазени economics: [independent review](../research/reports/independent_engine_review.json). Този исторически review използва пълните локални журнали; не се представя като изпълнен само върху public clone. Поправените 90 икономически сценария пазят всички оригинални решения/fills/complete net: [replay](../research/reports/final_economic_replay_verification.json). Окончателните публични V2 проекции са преизпълнени детерминирано за [90 основни](../research/reports/public_v2_90_replay_verification.json) и [39 допълнителни](../research/reports/public_v2_39_replay_verification.json) сценария. [Независимият publication audit](../research/reports/publication_verification.json) потвърждава всички 219 current/archived двойки без загуба на финансови записи.

Предупрежденията `f_fast/f_reversal` са анализирани, а конкретните history reads са проверени с 6 445 извиквания на всеки от четирите клона и нула несъответствия в запазения Pine probe. Предупрежденията остават; няма механичен refactor на stateful извиквания. Ограниченията на това доказателство са в [baseline audit](BASELINE_AUDIT.md).

## C. PERFORMANCE — запазени положителни и отрицателни резултати

| Реален M15, 02.08.2026 22:00–09.11.2026 02:45 UTC | Baseline | Нов UI |
|---|---:|---:|
| Цели позиции / native части | 72 / 212 | 72 / 212 |
| Net USD | −144.492 | −144.492 |
| PF на цели позиции | 0.905096 | 0.905096 |
| Native equity max DD USD | 842.255 | 842.255 |
| Начислени такси USD | 75.600 | 75.600 |

Условията са OANDA:XAUUSD, native RAW quantity 3, commission 0.175/unit/fill, slippage 2 ticks, default OHLC детализация, on-bar-close, next-tick fills. Това не са PU Prime lots или действителни брокерски изпълнения. 212 части не са независими позиции.

39 губещи кампании са първоначални стопове преди TP1. В комбинирания native портфейл FAST дава 49 позиции/−257.245 USD, REVERSAL 22/+91.934, RETEST 1/+20.819. Една retest позиция не позволява надежден извод за качеството на семейството. Изолираните модели и BUY/SELL/HTF разрезите са запазени в предишната диагностика и [baseline audit](BASELINE_AUDIT.md); сборът на изолирани модели не е еднопозиционният портфейл.

При common-window **23.10.2026 20:00–09.11.2026 02:30 UTC**, избран по налични данни и H1/H4 warmup преди резултатите, native entry cohorts са M5 **22/−79.700**, M15 **8/−4.445**, M30 **5/+62.724 USD**. Няма carryover/end-censored позиция в тези конкретни cohorts. Пълните различни прозорци дават други стойности; не се използват за избор на печеливш timeframe.

Python симулаторът не е доказано копие на Pine emulator. В неговия M15 BASE S3 дава +392.697 USD/14 позиции, но derived-M30 −354.229/5. S5 е +393.951/131, S8 +308.547/21; положителната сума не е доказана независима полза. Премахване на confirmation-candle gate променя S3 до 71/+83.434 и S5 до 151/+28.906 — повече сделки, по-слаб net. S8 без prior-pause gate е 22/+470.321, но е малък, предварително вече селектиран положителен sample. Нито този резултат, нито нулевите/блокирани опити се обявяват за победител.

Средният native net R е −0.102062, bootstrap 95% CI [−0.360261,+0.173557]. Win rate 33/72, Wilson 95% [34.83%,57.26%]. След обща Holm корекция на **129** регистрирани опита няма adjusted p<0.05. Това е exploratory multiple-testing анализ, не валиден сертификат за независимост/стационарност или поправка на вече видян holdout. Raw logistic Brier 0.280622 е по-слаб от константата 0.252461; calibrated 0.246092 върху 15 вече разглеждани позиции не установява edge.

Пълните таблици, cost/delay/exit разрези, всички губещи сделки, отворени количества, paid-cash DD, MAE/MFE граници и малки sample ограничения са в [EXPERIMENT_RESULTS](EXPERIMENT_RESULTS.md). Native equity DD не се заменя с Python paid-cash drawdown. Native cost stress запазва наблюдаваните fills: 1.5×fees/+1tick → −182.724 USD; 2×fees/+2ticks → −220.956 USD.

## D. NOT VERIFIED — конкретни граници

- Няма недокоснат окончателен holdout. Всички използвани календарни периоди вече са наблюдавани; 10.11–10.12.2026 е само бъдеща протоколна рамка. Не е създадена автоматична търговия или фонов тест за нея.
- Няма native strategy server alert: реалният Create опит е отказан поради **Basic: 0 technical alerts**. Отделен ценови INFO тест е доставен до TradingView toast в **18:02:51 UTC** и е Stopped—Triggered. Това не доказва strategy order-fill push или iPhone доставка.
- Няма физически iPhone тест или проверен активен SL/TP план в mobile viewport. В текущите пет рамки няма активна позиция; intraday Bar Replay е блокиран от Basic. Няма подменена със синтетичен сигнал снимка.
- Няма PU Prime account-specific contract/tick/volume/stop specs, broker bid/ask/ticks, spread/swaps/slippage history, margin validation или live execution. Offline risk calculator остава fail-closed при този непълен профил.
- Няма point-in-time news/consensus/macro/intermarket dataset. News pause е ръчен; няма работещ икономически календар, sentiment AI или твърдение за постоянна DXY/yield връзка.
- Няма numeric Deflated Sharpe/PBO, сложен neural/HMM/XGBoost deployment или независимо валидирано regime routing. Малкият sample и липсващата независима equity/label история не оправдават такива твърдения. S7 е opening-range breakout; отделна opening-range failure стратегия не е реализирана. [Пълен обхват по точки](MISSION_COVERAGE.md).

## E. CODE — поддръжка и възпроизвеждане

Генератор: `python unified/tools/build.py --check`. [Research README](../research/README.md) съдържа точните CLI, нови output paths и CPython 3.12.14 / `tzdata==2026.6`. Original economic source hashes, registrations и amendments не се презаписват. Проверяващият replay отказва друг dataset/source hash; новите M5/M30 данни трябва да бъдат налични локално, за да се повторят съответните actual-TF симулации.

Новите пълни OHLC/log exports и пълните журнали с reference price на всяка свещ остават локални. Публичната производна версия отстранява `reference`, `initial_stop`, `structure_target` от **всички decision rows**, пазейки всеки ред, candidate ID/status/reason и всички реално симулирани plan/trade/exit цени, такси, количества и загуби. Цените на отхвърлените кандидати остават в пълния локален журнал. Обединяването на първоначалния по-слаб public вариант разкриваше около 45% от close историята; затова политиката е засилена преди публикуване. Manifest свързва точните full/public hashes. Това е ограничение на разпространението на данните, не подбор по печалба. Първоначалният вече проследяван baseline архив остава непроменен.

Точният commit и compare URL се предоставят след успешно публикуване/прочитане обратно; този отчет не измисля предварително commit ID. Няма merge в main.

## F. RECOMMENDED CONFIGURATION — само изследователско наблюдение

OANDA:XAUUSD, стандартни Candles, M15 за възпроизводимия baseline; M5/M30 са допълнителни наблюдения без доказано преимущество. Запазени champion Inputs/Properties и разходи; STANDARD или MINIMAL, history/zones/debug OFF, потвърдени H1/H4 контексти ON. Новите S1–S10/model/routing flags OFF. Не използвайте quantity 3 като PU Prime lot размер. Няма основание да се активира challenger или да се разхлабват production филтри.

## G. REMAINING RISKS

Загубите на baseline са реални симулирани резултати и остават видими. Source parity и красив интерфейс не дават статистическо предимство. Основните нерешени рискове са малки и зависими samples, избор след наблюдение, OHLC intrabar ambiguity, разлика между Pine и Python изпълнение, provider revisions/initialization, реални spread/gap/financing разходи и различни брокерски котировки. DEBUG историята и native trade markers могат да се припокриват; числов R:R не е отделен dashboard ред. Platform repainting предупреждението е документирано, а не премълчано. Няма гаранция за положителен резултат.
