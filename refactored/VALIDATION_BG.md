# Какво действително е проверено

## Статуси

| Област | Резултат | Доказателство/ограничение |
|---|---|---|
| CODE_TESTS | PASS в наличната локална среда | 85 unittest проверки, 0 failures/errors; `reports/tests.json` и пълният log |
| BUILD | PASS | Детерминирано генериране; повторен build със същите байтове; общо ядро с един SHA256 |
| BASELINE | PASS | Git blob съвпада с посочения оригинал; baseline не е редактиран |
| PINE_COMPILE | PINE_COMPILE_UNVERIFIED | Няма наличен/използван реален Pine компилатор |
| EXECUTION_PARITY | UNVERIFIED / KNOWN_DIFFERENCES | Source core е еднакъв; runtime candidate parity и native/custom fill parity не са проверени |
| OUT_OF_SAMPLE | PERFORMANCE_UNVERIFIED_MISSING_DATA | Няма предоставени реални барове/логове/период и валиден отделен test dataset |
| FORWARD_TEST | NOT_RUN | Няма live/demo поток или наблюдавани брокерски изпълнения |
| MOBILE_UI | HTML_MOCK_CHECKED; TRADINGVIEW_UNVERIFIED | Три Chromium viewport макета; не са Pine screenshots |

Не е изчислена нова печалба за XAUUSD. Не е доказано подобрение спрямо −13.43R. Синтетичните числови примери в reports/synthetic_functional са тестове на аритметика/порядък на събитията, не пазарни резултати.

## Baseline срещу новото изпълнение

Съхранен е целият baseline, включително губещите настройки. Не е наличен baseline runtime event stream, затова не е правена измислена визуална parity проверка върху реални сделки.

Новото изпълнение запазва трите семейства и основните им числови прагове, но променя изрично lifecycle и execution semantics: независима откриваемост, family cooldown, истински nearest obstacle, последователен FVG, outward stop rounding, фиксирани signal-close targets, timeout next-open, отделни raw/filtered книги, calendar reset. Следователно историческият R може да се промени по легитимни причини. Не твърдим, че всички стари резултати са запазени или че новите ще са по-добри.

Няма настройване на RSI/ADX/ATR за „повече сигнали“, подбор на печеливш период или backdating. Основният риск от отрицателна expectancy остава въпрос за реални данни, не е „решен“ с интерфейс или статистически стоп.

## Изпълнени проверки

`python tools/run_tests.py` покрива:
- baseline hash, повторяем build, byte-identical canonical core;
- custom long/short огледални fills, SL-first, gapSL, gapTP, невалиден open;
- TP1/2/3 fractions, repeated touches, cost allocation, funding duration;
- next-bar lock и невъзможност за ретроактивен same-bar lock;
- timeout queued на close и изпълнен на следващо open;
- правилни zero/open/closed/cancelled резултати;
- независими книги, конфликт/приоритет и липса на data starvation от FILTERED pause;
- shield minimum sample, causal hysteresis, countertrend observation без унищожаване на RAW;
- референтен lifecycle със stable id, без same-bar пресъздаване;
- alerts master/category и запазване на няколко едновременни events;
- лексикални Pine проверки, функции/скоби, HTF offsets, малък брой plot calls;
- CSV/JSON валидатор, невалидни/непълни данни, duplicate events, candidate comparison.

**Граница на доказателствата:** Python execution референцията не изпълнява Pine `ta.ema`, `ta.rsi`, `ta.dmi`, request.security или TradingView state rollback. Lifecycle тестът е самостоятелна Python спецификация. Статичните проверки не разрешават Pine type qualifiers, семантика или реален resource budget. Тези неща са изрично непроверени.

## Мобилна проверка

`reports/mobile_mock.json` съдържа реално измерените HTML bounding boxes при 390×844,430×932,1440×900. Проверени са пет реда и липса на document/cell overflow за показания пример. Макетът използва HTML/CSS, не Pine renderer. Има ясно означение „МАКЕТ — НЕ TRADINGVIEW“. Не е доказано, че всяка стойност/настройка/font size ще изглежда идентично в приложението.

## Оставащ точен приемателен checklist

1. Компилирай индикатора и стратегията в реалния Pine Editor. Запази точния buildhash, всички errors и warnings. Не приемай зелен status без лог.
2. Отвори стандартни M5/M15/M30/H1/H4, провери invalid timeframe и HTF равна/по-ниска. На отворена свещ не трябва да се създава окончателен historical model marker.
3. Създай контролирани Bar Replay случаи: sweep→MSS→retest, противоположен пробив, reversal candidate/rejection, same-barSL/TP, gaps и partial exits.
4. Използвай еднакви настройки/данни за двете версии. Експортирай full CONFIRMED events и сравни с `tools/compare_events.py`.
5. Сравни custom RAW и native trade-by-trade. Обясни всяка разлика от slippage/commission/stop-first/order reservation/Bar Magnifier/gap rejection; не подменяй резултата.
6. Провери три отделни model runs и комбиниран портфейл с фиксирани costs и период. Проведи predeclared chronological validation/test и stress, без да гледаш test за пренастройване.
7. Направи реални TradingView screenshots на iPhone portrait и desktop при default12pt, по-голям16pt, без активен план и с близки TP/SL нива; проверка на последните свещи и text overlap.
8. При успешни проверки проведи forward demo наблюдение. Няма автоматично разрешение за live trading от този пакет.
