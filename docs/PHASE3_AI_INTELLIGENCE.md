# Фаза 3 — Причинни данни и локално ML изследване

`LOCALLY_TESTED`. Реално са обучени и изпълнени фиксирани logistic модели, validation-only Platt калибратори и ridge benchmark за очакван net R. `BLOCKED_EXTERNAL` за независима бъдеща оценка.

## Наличност на входа

Един ред съответства на оригинален plan ID и носи signal close, краен момент за availability на label и хеш на източниците чрез manifest. Входът за inference включва посока, подписана част на тялото, подписано отстояние от четирибаровата граница спрямо ATR, ADX, ATR спрямо 40-bar средна, подписан RSI, native efficiency, native HTF съгласуваност и начална стоп дистанция. Изчисленията използват оригиналните submitted полета и затворените свещи. Realized net, TP/SL fills и actual entry остават в отделната оценка. H1/H4, swings и timestamps се изчисляват отделно във feature библиотеката. Spread и bid/ask остават липсващи. [Входове, източници и хешове](../research/ultimate/reports/manifest.json).

Класовете са положителен net след платени такси и TP1/TP2/TP3 преди първи SL. При SL и искания TP в една свещ label остава неизвестен. Измерени са 33/72 net-positive, 32/72 TP1, 16/72 TP2 и 10/72 TP3 положителни labels. Моделът класифицира изхода на избрани исторически планове. Regime и volatility са наблюдавани причинни категории от Phase 4. За прогнозиране на бъдещи категории е необходима предварително определена цел и независима оценка. Отделен entry quality label или надеждна обучителна извадка за invalidation липсват.

## Времево разделяне и действителна оценка

При всяка цел split е 43 train, 14 calibration и 15 по-късни оценъчни кампании. Purge изключва labels, достигащи границата на следващия дял минус 15 минути. Label availability използва най-късния възможен close на последната native exit свещ. Скалирането и logistic weights се обучават само върху train. Platt използва прогнозите от отделния calibration дял и изисква поне 8 реда и двата класа. Отделният expanding walk-forward започва след 30 наблюдения, в блокове от по 12 оценъчни кампании. Пазят се model identity, weights, scaling, train hash, inference latency, вероятности, calibration bins, log loss, precision/recall и feature drift. [Действителни обучителни и inference артефакти](../research/ultimate/reports/phase3_models.json) · [Методика за оценяване на калибрация](https://scikit-learn.org/stable/modules/calibration.html).

| Оценъчна цел | Brier константа | Logistic | Platt |
|---|---:|---:|---:|
| Net positive | 0,25246 | 0,24732 | 0,23865 |
| TP1 преди SL | 0,25246 | 0,24732 | 0,25451 |
| TP2 преди SL | 0,13679 | 0,12960 | 0,10992 |
| TP3 преди SL | 0,08257 | 0,10027 | недостатъчни validation класове |

При net-positive цел raw logistic с фиксиран праг 0,5 приема 5 кампании с +36,375 USD, но средният net R е −0,1521. Калибрираният модел приема 0. По-късната извадка от 15 съдържа −114,861 USD, средно −0,3419 R при прием на всички. Това е подбор върху оригиналните fills с историческа заетост. Raw logistic е по-слаб от train константата в три от четирите walk-forward блока за net-positive. По-ниският Brier в един split е недостатъчен критерий за внедряване.

Фиксираният ridge benchmark използва същите point-in-time характеристики и цел net R. На 15-те кампании MSE е 1,00046 срещу 1,07081 за средната на train. Разликата 0,07035 има описателен 95% block-bootstrap интервал [−0,17139, +0,32923]. При прогноза над нула остават 7 кампании с −51,256 USD и −0,1615 средно R. В три от четирите walk-forward блока ridge има по-висока MSE от константата. [Предварителна регистрация на follow-up](../research/ultimate/registration_expected_r.json) · [Резултати и прогнози](../research/ultimate/reports/phase3_expected_r.json).

## Интеграция

Сегашните модели са офлайн research артефакти. Random forest, XGBoost, LightGBM, HMM, MoE, TFT, N-BEATS, DeepAR, autoencoder, RL, contextual bandit и Bayesian updating остават research-only заради 72 зависими, предварително селектирани наблюдения и липсващ независимо измерен период. За production inference са необходими проверен входен schema, версия на модела, проследен timestamp и отделен risk veto. Pine работи в своя execution model. Външният модел може да анализира alert payload в самостоятелна услуга. Няма осъществена Pine или брокерска интеграция на ML. [TradingView alerts](https://www.tradingview.com/pine-script-docs/concepts/alerts/).
