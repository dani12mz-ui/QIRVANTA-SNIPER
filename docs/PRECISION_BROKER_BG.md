# Precision & Risk · PU Prime и чувствителност към разходи

**BLOCKED** за точните условия на `PU Prime MT5 XAUUSD.s`. Проверка от тази среда откри 0 работещи пътя до MT5 терминал и липсващ Python MT5 SDK. Няма прочетени договор, tick value, tick size, минимален lot, lot step, реален bid/ask, комисиона, маржин, stops level, swap по посока или fill policy за конкретния сървър и сметка. Брокерът описва суфикси според вида сметка и публикува ориентировъчен gold spread в pips. Тези публикации не свързват ориентировъчния spread с проверена ценова стойност на `XAUUSD.s` за тази сметка. [Суфикси на PU Prime](https://helpcenter.puprime.com/hc/en-001/articles/17043145073935-Why-am-I-unable-to-trade-certain-products-or-symbols) · [Ориентир за спред](https://helpcenter.puprime.com/hc/en-001/articles/360004362035-What-is-the-average-spread-on-Gold)

**EXPERIMENTAL** за чувствителността на фиксираните архивни M5 изпълнения на FAST и S5 към различни разходи. Ценовият поток и заетостта са от OANDA архива. Трите PU сценария са явно зададени в USD за една симулирана единица, без MT5 lot конверсия. Заменяме вече начисления пълен спред 0,35 и отчетената комисиона, след което начисляваме само промяната спрямо вложеното 0,002 slippage за пазарните входове и непазарните TP алтернативи със стоп/market изход. В лимитните TP части няма добавен slippage при изход. Rollover е условен разход само когато частта е установено отворена през 17:00 New York, с тройно тегло в сряда. Това е допускане по публичното описание. [PU Prime swap](https://helpcenter.puprime.com/hc/en-001/articles/360004361735-What-is-Swap-Fee) · [Машинен отчет](precision_evidence/broker_sensitivity.json)

| Разходен сценарий | Spread / slip / round-trip fee / rollover на единица | FAST net / DD USD | S5 net / DD USD |
|---|---:|---:|---:|
| OANDA референция | 0,35 / 0,002 / 0,35 / 0 | −160,40 / 272,37 | +1,63 / 145,76 |
| Нисък условен | 0,30 / 0,03 / 0 / 0,05 | −30,48 / 202,89 | +104,90 / 136,30 |
| Централен условен | 0,50 / 0,10 / 0,35 / 0,10 | −276,73 / 346,56 | −91,88 / 154,51 |
| Стрес | 0,80 / 0,20 / 0,50 / 0,20 | −504,98 / 560,74 | −274,58 / 286,80 |

За проверим размер са нужни метаданните и котировката от разрешения съществуващ терминал. Оценката използва загубата от вход до изпълним SL в тикове, неблагоприятната tick value, комисията и допускането за gap/slippage. Spread участва веднъж чрез двустранните цени или отделна оценка, според предоставените цени. Допустимият lot се закръгля надолу по broker step и се проверява спрямо минималния размер, стоп дистанцията и свободния маржин. Ливъриджът участва в проверката на маржина. Текущият отчет оставя размера за реалния брокер неопределен. [MT5 symbol info](https://www.mql5.com/en/docs/python_metatrader5/mt5symbolinfo_py) · [MT5 bid/ask](https://www.mql5.com/en/docs/python_metatrader5/mt5symbolinfotick_py)

```powershell
python -B -m qirvanta_ultimate.mt5 probe
python -B -m gold_precision.broker --tape ../../outputs/gold_pro/archive_release_tape.json.gz --data-root ../mission --logs-dir ../tv-final --out precision-broker-check.json
```
