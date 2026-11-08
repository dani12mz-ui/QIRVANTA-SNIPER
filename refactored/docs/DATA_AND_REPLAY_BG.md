# Данни, журнал и възпроизводима проверка

## Какво има и какво няма

В пакета няма истински XAUUSD OHLC/tick данни. Числата от screenshot са контекст, не набор за обучение/тест. `tests/fixtures/synthetic_*` са измислени цени 100/90/110 за unit tests, с изричен manifest `dataset_kind=synthetic`.

`tools/validate.py` **не генерира Pine сигналите наново в Python**. Той приема експортирания поток CONFIRMED кандидати и OHLC, проверява времената и преизпълнява описания консервативен execution model. Това е условна проверка на изпълнението. За реално signal backtest използвай `QIRVANTA_SNIPER_VALIDATION.pine` в TradingView и пълния Strategy Tester export.

## Входни файлове

### OHLC CSV

Колони: `time,time_close,open,high,low,close`. Времето е Unix **милисекунди** или ISO8601 с изрична зона. Един ред за един стандартен бар, строго хронологично, без припокриване. Не добавяй synthetic fill за липсващ уикенд/сесия. Цените трябва да са крайни числа; low≤open/close≤high.

### Pine events JSONL

Включи `JSONL събития в Pine Logs` и използвай пълните записи. Платформата ограничава историята на логовете; непълно копие не е пълен trade log. Валидаторът приема и начална текстова част пред първия JSON знак на лог реда. Отхвърля дублиран event_id.

CONFIRMED / CORE съдържа: `plan_id`, `family`1/2/3, `side`±1, `symbol`, `timeframe`, `known_time`, `effective_time`, `reference`, `initial_sl`, `tp1/2/3`, `atr_at_signal`, `roundtrip_cost_price`, `max_risk_atr`, `obstacle`, `required_room_r`, `countertrend`. Техническият кандидат трябва да е известен точно на затварянето на съответния OHLC бар. Не може да се премести към по-ранен връх.

Останалите Events разграничават WATCH, CONFIRMED, REJECTED, CANCELLED, EXPIRED, QUEUED, CAPACITY, SIM_ENTRY, SIM_TP1/2/3, SIM_SL, SIM_TIME, STOP_UPDATE, AMBIGUOUS, CLOSED, EVIDENCE и native събитията. При intrabar touch `effective_time` е null, известното време остава close. JSON имената на полетата са технически; потребителските причини са на български.

### Manifest

Копирай `data/MANIFEST_EXAMPLE.json`, попълни действителните symbol/provider/timeframe, core SHA256, всички Pine inputs, config на execution референцията, източник и проверка на пълнотата. `coverage_complete=false` умишлено отказва изпълнение. За real manifest са задължителни валиден SHA256 и непразна конфигурация. Твърдение `coverage_complete=true` не е независимо доказателство за произход — запази оригиналните експорти и hashes.

## Команди

```bash
python tools/build.py
python tools/run_tests.py
python tools/validate.py --bars data/ohlc.csv --events data/pine_events.jsonl --manifest data/manifest.json --out reports/real_run
python tools/compare_events.py data/indicator_events.jsonl data/strategy_events.jsonl --out reports/candidate_parity.json
```

Сравнителят проверява stable plan ids, family, direction, времена и всички нива. Празни два файла НЕ са успешна parity проверка. Различните execution Events не се прикриват чрез съвпадащ краен сбор R.

Хронологични раздели (избери границите **преди** настройване):
```bash
python tools/validate.py --bars data/ohlc.csv --events data/pine_events.jsonl --manifest data/manifest.json --train-end 2025-01-01T00:00:00Z --validation-end 2025-07-01T00:00:00Z --out reports/split_run
```

Датите са пример за синтаксис, не избран период за QIRVANTA. Границите трябва да са вътре в истинските данни. Сделки, пресичащи граница, се изключват от метриките на отделните раздели и се броят отделно; общият отчет ги запазва. Evidence историята се пренася хронологично, само от известни минали затваряния. Не е правена оптимизация или подбор на „добри“ дни.

## Изход

`metrics.json`, `trades.jsonl`, `trades.csv`, `events.jsonl`. Сделките съдържат id/model, времена, actual modeled open, initial stop, фиксирани targets, частични количества/цени, gross/cost/net R, финално основание, evidence при сигнала. JSONL пази и всички partials, CSV е плосък обзор.

Метрики: closed, positive/negative/zero, gross/cost/net, average net, profit factor с explicit NO_LOSSES/NO_DATA, closed-equity maxDD, average win/loss и holding bars. **Това е drawdown по реализирани кампании, не пълен intrabar mark-to-market drawdown.** Отворените позиции и cancelled заявления се отчитат отделно.

Има описателен moving-block bootstrap за средно R при ≥40 приключили сделки: блок5,1000 повторения,seed42. Това са предварително избрани диагностични настройки, не гаранция за статистическа независимост или бъдеща печалба.

Cost stress default1×,1.5×,2× използва същия технически cohort. Ако по-високият разход наруши валидността на plan риска, не произвежда фалшива печалба, а `CANDIDATE_RISK_LIMIT_EXCEEDED`. За пълна оценка при нови разходи повтори Pine откриването на кандидатите със същите нови настройки; не се преструвай, че условният replay е това.

## Независими модели и портфейл

Тествай `Модел за този тест` отделно за всяко семейство и после Всички. Използвай еднакви барове, warmup, дата, costs и corehash. Проверявай M1/M2/M3 и разликата RAW/FILTERED. Никога не заключавай, че премахване на един губещ ред от общата таблица просто изважда неговото R: заетостта и приоритетът на общия портфейл могат да променят бъдещите fills.
