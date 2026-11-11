# QIRVANTA GOLD PRO · Посочни наблюдения

Таблото показва две независими състояния. **DIRECTIONAL OPPORTUNITY** описва какво е засечено. **TRADE PERMISSION** показва решението на S5/RC2 и състоянието на симулирания RAW Paper слот. Затова потвърден SELL и WAIT могат да стоят едновременно.

1. Копирай целия [Pine v6 файл](QIRVANTA_GOLD_PRO_SELL_VISIBILITY.pine) в Pine Editor и го добави на стандартна графика **OANDA:XAUUSD**, M5, M15 или M30.
2. В **Inputs → DIRECT TREND BREAKOUT · SHADOW** включи **Покажи наблюдение**. В **PREMIUM** потвърденият shadow пробив се вижда в лявата карта. Ако има противоположно активно наблюдение, **CONFLICT** показва и двете посоки и източници. Контратрендовият S5 WATCH има отделен етикет.
3. Часът до източника е UTC. Възрастта се отчита спрямо текущото време в LIVE и спрямо избраното затваряне в **HISTORICAL REVIEW**. Подсказките показват точния UTC час, срока, модела и пълната причина. Пробивът остава видим според съществуващия му жизнен цикъл.
4. **RESEARCH** обозначава диагностичен кандидат. Entry, SL и TP се показват при валиден S5 READY план. **POSITION OCCUPIED · S5 RAW · PAPER** отразява заетия виртуален слот на индикатора. За исторически преглед избери **FULL AUDIT → Исторически преглед** и въведи точния час на затваряне.

За отделно наблюдателно известие включи **Inputs → DIRECTIONAL OBSERVATIONS → Известия за потвърден shadow пробив**, след което избери **RESEARCH · SELL BREAKOUT CONFIRMED** или **RESEARCH · BUY BREAKOUT CONFIRMED** в **Create Alert → Condition** и **Once Per Bar Close**. Текстът съдържа инструмента, интервала, посоката и UTC време на затваряне в милисекунди. Известието е за изследователско наблюдение. Съществуващият поток **Any alert() function call** запазва S5/RC2 payload-ите си. [Официални условия за Pine известия](https://www.tradingview.com/pine-script-docs/concepts/alerts/)

[Нативен LIVE кадър със SELL DETECTED и WAIT](evidence/screenshots/live-M30-sell-detected-wait.jpg) · [Проверки, исторически примери и сурови журнали](NATIVE_QA.md)
