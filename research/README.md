# QIRVANTA research — офлайн, всички кандидати OFF

Това е причинна Python изследователска библиотека, отделна от единствения TradingView Pine script. Не изпраща broker/MT5 поръчки, не включва автоматично challenger и не доказва доходност. Положителен резултат върху вече видени данни не е независимо потвърждение. S6 е недостъпен без доказан volume/VWAP; S10 отказва избор без проверен независим validation manifest.

## Код и документи

| Път | Действително реализирано |
|---|---|
| `data/` | Строг loader, оригинален dataset manifest, точна M15→M30 агрегация, local-only Pine Logs extractor. |
| `features/causal.py` | EMA/ATR/ADX/RSI/efficiency, забавени потвърдени pivots, regime, завършени H1/H4, London/NY DST opening ranges. |
| `strategies/library.py` | Независими фиксирани S1–S9 state machines и отказващ без доказателства S10 routing. |
| `strategies/ablation.py` | Само изрично регистрирани gate ablations, изолирани чрез context manager; без промяна на основната library. |
| `backtesting/engine.py` | Следващ-open fills, един портфейл, partial exits, stop-first ambiguity, paid-cash и closed-part отчетност. |
| `labeling/`, `models/` | Offline triple-barrier labels и малък logistic baseline; не работят вътре в Pine. |
| `validation/` | Chronological purge/embargo, train-only скалиране, validation-only calibration, bootstrap/Holm, възпроизвеждане и контролирани сравнения. |
| `risk/calculator.py` | Само примерна сметка при проверени спецификации и актуален bid/ask; unknown/stale inputs блокират. Няма order dispatch. |
| `reports/` | Регистрации, freezes, всички успешни/нулеви/отрицателни резултати и запазена история на поправките. |
| `tests/` | Синтетични regression проверки; отделни реални replays са записани като replays, не допълнителни unit tests. |

Точните определения са в [STRATEGY_INVENTORY](../docs/STRATEGY_INVENTORY.md), механизмите и ограниченията — в [RESEARCH_ARCHITECTURE](../docs/RESEARCH_ARCHITECTURE.md), а резултатите — в [EXPERIMENT_RESULTS](../docs/EXPERIMENT_RESULTS.md).

## Runtime и проверки

Проверено с **CPython 3.12.14 / Windows AMD64**, стандартната библиотека и IANA timezone provider **tzdata 2026.6**. Dependency pin: [runtime_requirements.txt](validation/runtime_requirements.txt). Не са инсталирани пакети. За възпроизвеждане на друга система използвай същия timezone provider, а не различни OS DST правила; recorded runtime има празен `zoneinfo.TZPATH`. Exact source hashes са в [final_reproduction_manifest](reports/final_reproduction_manifest.json) и проверките.

Командите са от repository root:

```text
python -B -m unittest discover -s research/tests -p "test_*.py" -v
python -B research/backtesting/verify_engine.py
```

`verify_engine.py` записва текущите [tests.json](backtesting/tests.json) и [tests.log](backtesting/tests.log), включително hashes на implementation/test файловете. Тези проверки не са Pine компилатор, native parity или доказателство за печалба.

## Непроменими данни и replay

Първите 80 M15 опита използват оригиналните 6 445 вече наблюдавани бара в `diagnostics/protocol/datasets/B-ALL-FULL/bars.csv`, SHA `7015f6386d2627237c57ff8625d59cbe3c9b852e4742b786cbb4679db3d48c56`. Те не се подменят с по-нов feed. Допълнителните 10 derived-M30 опита използват 3 222 точни двойки от същия архив; последната непълна половина остава отбелязана. Това не е нов OOS период.

Първоначалните run scripts са еднократни freezes. За проверка на окончателните **90** записани икономически журнала използвай нов output файл:

```text
python -B -m research.validation.replay --output ../mission/replay-check-new.json
python -B -m research.validation.replay --output ../mission/replay-executed-new.json --execute
```

Първата команда проверява hashes/отчетността; `--execute` преизпълнява всички записани конфигурации и изисква пълно детерминистично съвпадение. Съществуващ output се отказва. Поправките на entry-fee timing и малките bootstrap извадки не се броят като нови хипотези; оригиналните източници/журнали са запазени в `reports/method_history/`.

Допълнителните реални M5/M15/M30 datasets и gate/parameter ablations имат отделни регистрации и отчети: `actual_tf_registration.json` / `actual_tf_results.json`, `ablation_registration.json` / `ablation_results.json`. Проверявай статута и общия прозорец в тях; 90-журналният replay не твърди, че включва тези допълнения. За реалните TF данни е необходим локалният dataset със записания hash.

## Нов действителен platform export

Новите full OHLC данни остават локално извън Git: правата за разпространение не са установени. Extractor не презаписва директории и отказва output вътре в repository. Примерът изисква действителния наблюдаван source hash, не произволен string:

```text
python -B -m research.data.extract_platform ../mission/M5_NATIVE.pine-logs.csv ../mission/M5_dataset_new --timeframe 5 --expected-source-sha256 92307768be68946dc87cbe4ea45a8b5e32446fd3b7a0c1a38dce40dffe358be3
```

Допълнително `--expected-log-sha256` проверява точно изтегления CSV. Допустимите timeframes са `5`, `15`, `30`. Изходът е `bars.csv` + metadata-only `dataset_manifest.json`; запазват се всички барове и gaps. Native P&L се проверява отделно чрез immutable accounting auditor; извличане на OHLC не удостоверява сделките. M5 никога не се реконструира от M15.

## Конфигурация без скрити включвания

```python
from research.data.loader import load_bars
from research.backtesting.engine import BacktestConfig, run_backtest

config = BacktestConfig()  # enabled_families=(): всички експерименти OFF
# Само отделен предварително описан research run:
candidate = BacktestConfig(enabled_families=('S1',), exit_variant='PARTIAL_R')
result = run_backtest(load_bars(), candidate)
```

Всички текущи `BacktestConfig` полета:

| Полета / defaults | Смисъл |
|---|---|
| `enabled_families=()` | Изрично избрани S1–S10; няма автоматично активиране. |
| `strategy_params={}` | Отделни `StrategyParams` по family; пълните прагове са в library manifest и инвентара. |
| `commission_per_unit=.175`, `slippage_ticks=2` | Симулационна комисиона/unit/fill и adverse market/stop slippage; не се удвояват повторно в cash. |
| `mintick=.001`, `quantity=3`, `pointvalue=1`, `symbol='OANDA:XAUUSD'` | Единици/идентичност на симулацията, не broker lots или проверени PU Prime спецификации. |
| `exit_variant='PARTIAL_R'` | Опции PARTIAL_R, FIXED_R, STRUCTURAL, BREAKEVEN, ATR_TRAIL. |
| `target_rs=(1,2,3)`, `fixed_target_r=2` | R проекции; STRUCTURAL изисква действително известно препятствие и може да откаже. |
| `max_hold_bars=48`, `entry_delay_bars=1`, `cooldown_bars=8` | Барови срокове; next-open, не вход със задна дата. |
| `max_entry_deviation_atr=.2` | Маркира вече изпълнен невалиден gap и заявява последващ exit; не изтрива fill. |
| `daily_loss_limit_cash=None`, `max_risk_cash=None` | Незадължителни paid-cash daily/planned-risk ограничения; OFF по подразбиране. |
| `manual_news_pause=False` | Явна ръчна пауза; не е live calendar. |
| `validation_manifest=None` | S10 не маршрутизира без проверено evidence. |
| `volume_kind='UNVERIFIED'`, `verified_bar_vwap=False` | S6 блокира без EXCHANGE_TRADED семантика и доказан `bar_vwap`; положителен tick volume не е достатъчен. |
| `trade_start_ms=None`, `trade_end_ms=None` | Ограничават нови потвърждения; не режат warmup и не затварят насила остатъчните позиции. |

Standalone CLI има `--data`, повторяем `--family`, `--exit-variant`, задължителен `--output` в нов файл. Останалите настройки са Python API; не се обещават несъществуващи CLI flags. `candidate_provider` е отделен synthetic test seam, ясно маркиран в резултата; не е разрешение за future-looking сигнали.

Платената cash крива, net на затворените части и недостъпният native mark-to-market equity са различни метрики. OPEN/PENDING, неплатените в closed-part основата entry fees, ambiguity и всяка причина за отказ остават видими. Няма автоматично включване на модел, външен AI в Pine, broker bridge или насрочена реална търговия.
