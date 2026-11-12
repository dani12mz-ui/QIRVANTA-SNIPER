# QIRVANTA GOLD PRO · H4 DATA MISSING

**PASS.** Поправката се компилира нативно в TradingView и премина точен паритет с `6e2c2b4` на OANDA:XAUUSD M5, M15 и M30. Публикуваният пълен Pine има SHA-256 `a9955a1ed3f402f1b8674b3ef2fabfc3d6578bb20f61d8a7d12ade5f527353a2`. [Pine v6](QIRVANTA_GOLD_PRO_HTF_DISPLAY_FIX.pine) · [Компилация](evidence/compile.jpg) · [Минимална поправка](DISPLAY_ONLY.patch)

Визуалният snapshot изисква налични време, цена, EMA50 и EMA50 отпреди две HTF свещи за всяка рамка. При липса пази `na` и показва `H4 DATA MISSING` в реда за тренда и в MARKET BIAS. Контекстната оценка показва `HTF DATA MISSING`. При налични входове показва досегашните `UP`, `DOWN` или `MIXED`. Промяната е 11 добавени и 6 премахнати реда. Отделният пълен Pine файл позволява базата и нейните проверки да останат достъпни в оригиналния си вид.

| Състояние | OANDA:XAUUSD, затваряне на 11.11.2026 UTC | Нативен кадър |
| --- | --- | --- |
| H4 DATA MISSING | M5 09:45 | [Преди](evidence/M5-before-missing.jpg) · [След](evidence/M5-after-missing.jpg) |
| H4 MIXED | M30 06:30 | [Изчислено MIXED](evidence/M30-H4-mixed.jpg) |
| H4 UP | M15 12:30 | [Изчислено UP](evidence/M15-H4-up.jpg) |
| H4 DOWN | M15 16:30 | [Изчислено DOWN](evidence/M15-H4-down.jpg) |

И двата M5 кадъра показват същия исторически `BUY READY` с Entry **4049.745**, SL **4046.108** и TP **4053.382 / 4057.019 / 4060.656**. Сменят се етикетите за H4 и качеството на контекста. На M15 в 12:30 H1 DOWN и H4 UP продължават да дават изчислен общ `MIXED`.

Нативният одит сравнява стандартни свещи при еднакви настройки от 00:00 до 17:30 UTC на 11.11.2026. Оригиналният и поправеният CSV съвпадат байт по байт за всяка рамка. Съвпадат всички CORE и READY записи, рискът, Entry/SL/TP, симулираното изпълнение, RAW PAPER P&L и alert payload-ите. За всеки интервал съвпадат и **1 199 затворени свещи** от Shadow журнала. [Оригинални нативни двойки](evidence/native-pairs.zip) · [Машинно четим QA](evidence/native-qa.json)

| Рамка | Първа заредена свещ UTC | Затваряния в одита | CORE / READY | RAW PAPER събития |
| --- | --- | ---: | ---: | ---: |
| M5 | 06.11.2026 00:20 | 211/211 | 42 / 6 | 32 |
| M15 | 26.10.2026 06:30 | 71/71 | 20 / 2 | 6 |
| M30 | 07.10.2026 05:30 | 36/36 | 0 / 0 | 0 |

M30 прозорецът няма CORE, READY или RAW PAPER събития. На него са проверени компилацията, нативните данни, Shadow наблюденията, EARLY WATCH и изчисленото H4 MIXED. Паритетът на изпълнение с действителни събития е упражнен на M5 и M15. Проверяваните известия са нативни payload-и в журнала.

Каноничният H1/H4 източник остава в `416cb2f`. Поправката ползва съществуващите входове в селектора и добавя нула `request.security` заявки. P1/P2 остават `RESEARCH ONLY`. Запечатаният forward протокол е сверен по оригиналния SHA-256. Графиките са проверени във временни табове на тестовия профил с изключен Autosave. [Независим каноничен източник](https://github.com/dani12mz-ui/QIRVANTA-SNIPER/blob/416cb2fef55b98ac09e762563055c297573e5962/research/professional_selector_htf/QIRVANTA_CONFIRMED_HTF.pine)

Възпроизвеждане от корена на репото с Python 3.11+.

```sh
git apply --check research/final_htf_data_integrity/DISPLAY_ONLY.patch
python -B -m zipfile -e research/final_htf_data_integrity/evidence/native-pairs.zip htf-display-native
python -B -m research.final_htf_data_integrity.qa --native-dir htf-display-native --output native-qa.json
python -B -m unittest discover -s tests
```
