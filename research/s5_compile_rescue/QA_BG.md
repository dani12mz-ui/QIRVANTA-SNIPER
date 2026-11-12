# QIRVANTA GOLD PRO · S5 + LUMINOUS

**VERIFIED** в TradingView на `OANDA:XAUUSD` с обикновени свещи на M5, M15 и M30. Пълният Pine v6 се компилира и се зареди на M5 след един опит за тази версия. M15 и M30 са изпълнени със същия зареден скрипт. SHA-256 `023c9eaf8a0e0085fd187b4bf9b5f76eb48017dd187c5e5b04cb8e542ff852ce`. [Финален Pine](QIRVANTA_GOLD_PRO_S5_LUMINOUS.pine) · [Нативна компилация](evidence/M5-combined-native-compiled.jpg)

## 1. Олекотяване на проверения S5

Преди редакцията фиксирахме `5528556584910faa75f1b76d2d96f14d3852e6e7`, хеша на Pine и оригиналните нативни архиви. Съществуващият QA архив е възпроизведен с точни 318 сравними затваряния. [Параметри и хешове](FROZEN_BASELINE.json)

| Елемент | 5528556 | S5-only | S5 + Luminous |
| --- | ---: | ---: | ---: |
| Редове | 3593 | 2646 | 2805 |
| Лексикални токени | 36 330 | 25 492 | 27 269 |
| Декларирани функции | 164 | 116 | 118 |
| Места с `for` / `while` | 45 / 2 | 35 / 2 | 41 / 2 |
| Места с `request.security` / `request.security_lower_tf` | 8 / 2 | 6 / 2 | 6 / 2 |

Броят на токените измерва изходния текст. Реалното доказателство за компилация са статусите `Compiled.` и `Added to chart.` в Pine Editor. [Пълен граф и точни диапазони на редакциите](evidence/source-graph.json) · [Статична сверка на единния файл](evidence/combined-static.json)

| Премахнат път | Доказателство за липса на активен потребител |
| --- | --- |
| Champion `S0`, алтернативните `S1–S4` и `S6–S10`, `F0–F10` | `tvStrategyMode` е фиксиран на `S5`. Остават същият `processBar`, изчистване на масивите и последователност BUY, SELL. `f_tvCandidate` специализира константните условия. Reclaim, срокът от 3 бара, геометрията, причините за отказ и идентификаторите остават. |
| Opening Range, VWAP, алтернативните EMA и legacy броячите | Потребителите им са в премахнатите модели или записват в диагностика без четене. London/NY визуалните разделители запазват активните си проверки. |
| Старо нативно управление, неизползвани визуални функции и предишни формати за съобщения | Няма достижим потребител от S5/RC2, наблюденията или таблото. `f_tvQtyOnGrid` остава в READY, както и действителните `alert()` и `alertcondition()` извиквания и payload-и. |
| Двете частни Wick HTF заявки и изключеният Wick QA журнал | Резултатите на двете заявки завършват в `wfH1Side` и `wfH4Side` без четения. `wfAuditBar` е константно false. Реалните Wick наблюдения, маркерите и жизненият им цикъл остават. Каноничният HTF контекст, Pressure и останалите QA входове остават. |

Графът проследява `processBar → f_tvCandidate → f_tvStart/Clear → f_makePlan/f_validPlan → f_planEvent`, после `f_pick/f_queue/f_step`, READY, RC2 alert и визуализацията. M2, FILTERED и RAW продължават да захранват eligibility, дневните лимити и счетоводния журнал. Запазени са всички слотове и редът на работа. Изпълнимите токени в другите запазени функции и всички техни сигнатури съвпадат. При интеграцията единствената редакция преди добавения зонален слой са квотите за етикети, линии и правоъгълници от 42/16/6 на 60/26/12. Luminous рециклира до 4 линии, 4 правоъгълника и 2 ценови етикета, плюс пръстен от 8 маркера. Добавени са 0 заявки, 0 таблици и 0 известия.

## 2. Нативни сравнения

Basic отказа интрадневен Bar Replay. Съществуващите архиви започват по-рано от текущата сесия. Затова в два временни таба заредихме точния `5528556` и изпитвания вариант и изравнихме `Calculated bars` във всяка двойка. Защитените сравнения изискват еднакъв първи бар, OHLC, warmup и настройки. Срезът за всички измервания е 11.11.2026 от 00:00 до 17:30 UTC. [Ограничение на Basic](evidence/M5-archive-replay-basic.jpg)

- **S5-only преди интеграцията** · M5 при 1203 изчислителни свещи. CSV файловете съвпадат байт по байт. Съвпадат 211 затваряния в одита, 1202 затворени свещи в Shadow префикса, 42 CORE, 6 READY и 32 RAW PAPER събития. В историческия snapshot на 09:45 UTC нативните DTV_REVIEW записи съвпадат и показват BUY READY с H4 DATA MISSING на графиката. На M30 при 06:30 UTC е видимо действително изчисленото H4 MIXED. [S5-only компилация](evidence/M5-slim-native-compiled.jpg) · [Сравнение](evidence/M5-slim-parity.json) · [Сурови журнали](evidence/slim-native-M5.zip) · [M5 преглед](evidence/M5-slim-review.jpg) · [M30 преглед](evidence/M30-slim-review.jpg)
- **S5 + Luminous** · Защитените потоци съвпадат след отделянето единствено на добавения `LZ_EVENT` поток. Проверени са CORE, READY, Entry/SL/TP, отказите, заетостта, RAW PAPER вход/изход, разходите, P&L, drawdown, Pressure OHLC, Shadow, Early Watch и alert payload-ите. M30 няма CORE/READY или RAW PAPER събития в този прозорец. [Сравнение](evidence/combined-native-parity.json) · [Сурови нативни журнали](evidence/combined-native-pairs.zip)

| Рамка | Първо заредено отваряне UTC | Calculated bars / Shadow префикс | Затваряния | CORE / READY / RAW | Нетно R / DD в R | Luminous |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| M5 | 06.11.2026 09:20 | 1205 / 1204 | 211 | 42 / 6 / 32 | −1.23099353 / 2.98907829 | 66 |
| M15 | 26.10.2026 14:15 | 1206 / 1205 | 71 | 20 / 2 / 6 | −0.04255369 / 1.03082818 | 13 |
| M30 | 07.10.2026 11:30 | 1207 / 1206 | 36 | 0 / 0 / 0 | 0 / 0 | 7 |

И двете страни на всяка двойка имат един и същ отчетен резултат. Различният warmup спрямо стария архив променя кумулативния RAW PAPER ledger, затова таблицата използва само новите съпоставими двойки. Данните упражняват технически паритет на индикатора. Реална търговия, доставка на известия и tick-volume режим на Pressure са извън този нативен срез.

Зоните ползват същите потвърдени 3/3 pivot, ATR14, FVG, origin и правила за touch, mitigation, rejection, replacement, invalidation и expiry. С отделния нативен архив на `2a4d2e3` съвпадат 86 събития по ID, ред, време на потвърждение, ценови граници, структурно ниво, време на pivot и FVG при еднакви OHLC в целевия прозорец. Началата на двете зонални серии са различни. Сверката важи за 318-те общи целеви затваряния. Маркерите и кутиите започват на потвърждението. В Historical Review обработката спира на избраното затваряне. [Проверен самостоятелен Luminous](https://github.com/dani12mz-ui/QIRVANTA-SNIPER/tree/2a4d2e3daee64e7b73e2f3a95fce6a1762aa3d07/research/luminous_zones)

## 3. Визуални проверки

| Графика | Кадър |
| --- | --- |
| M5 BUY REJECTION в 03:40 UTC | [Тъмен фон](evidence/M5-buy-rejection-dark.jpg) |
| M5 S5 BUY READY в 09:45 UTC с Entry 4049.745, SL 4046.108, TP1–3 4053.382 / 4057.019 / 4060.656 | [Светъл фон](evidence/M5-buy-ready-light.jpg) · [Зони изключени](evidence/M5-plan-zones-off-light.jpg) |
| M15 активна SELL зона в 12:00 UTC | [Тъмен фон](evidence/M15-sell-zone-dark.jpg) |
| M30 активни BUY и SELL зони в 12:00 UTC | [Светъл фон](evidence/M30-dual-light.jpg) · [Тъмен фон](evidence/M30-dual-dark.jpg) |
| M30 BUY инвалидация в 13:30 UTC | [Тъмен фон](evidence/M30-invalidation-dark.jpg) |
| M30 390 × 844 px | [MOBILE LIVE](evidence/M30-mobile-390-dark.jpg) · [FULL AUDIT с изрязване](evidence/M30-review-390-dark.jpg) |

При сгъстен M5 изглед зоналният етикет може да застъпи малък TP етикет на платното. Всички стойности се виждат в основното табло. `Show BUY / SELL research zones` изключва зоналните рисунки отделно, както показва сравнителният кадър. При 390 px MOBILE се побира, а наследеният FULL AUDIT се изрязва отляво. Всички проверки са в несъхранени тестови табове с изключен Autosave. `main`, двата проверени източника и запечатаният forward файл са съхранени.

## Възпроизвеждане

От корена на хранилището. За сравнението на зоните е нужен Git обектът `2a4d2e3`.

```sh
python -B research/s5_compile_rescue/build_slim.py S5_ONLY_SLIM.pine
python -B research/s5_compile_rescue/static_graph.py --candidate S5_ONLY_SLIM.pine --output slim-static.json
python -B research/s5_compile_rescue/static_graph.py --combined --candidate research/s5_compile_rescue/QIRVANTA_GOLD_PRO_S5_LUMINOUS.pine --output combined-static.json
python -B -m zipfile -e research/s5_compile_rescue/evidence/slim-native-M5.zip slim-native
python -B -m research.s5_compile_rescue.compare_native slim-native --frames 5 --output slim-parity.json
python -B -m zipfile -e research/s5_compile_rescue/evidence/combined-native-pairs.zip combined-native
python -B -m research.s5_compile_rescue.compare_native combined-native --combined --output combined-parity.json
python -B -m unittest discover -s tests
```

Преминаха **189** съществуващи теста. SHA-256 на изчисления S5-only за одита е `96c06840e856a079ed6a45840967ee0e1ec25ed5a9f86cf24bf83b9b9cb7de4a`.
