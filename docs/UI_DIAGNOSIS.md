# Диагноза на огромния текст

## Доказаният външен обект

На 09.11.2026 в Object Tree на текущия TradingView layout е намерен отделен **Text drawing**, различен от `QIRV ONE`. Неговият прозорец Text съдържа **115 724 символа Pine код**, шрифт 14. SHA-256 е `0a4e328ba1a29d591f2e4763adbaf4572a090215da202931e8380e21749354ba` — точно временния H1 editor buffer от предходната диагностика. Съдържа всички посочени фрагменти: `NATIVE_EXIT_GAP`, `NATIVE_SUBMITTED`, `plannedCampaignQty`, `maxDeviationATRFrozen`, `f_price(e.tp1)`, `e.reason`.

Това установява вида и съдържанието на обекта, но не установява кой и кога е извършил първоначалното поставяне. При paste с фокус върху графиката вместо Pine Editor може да се създаде такъв Text drawing. **Промяна на Pine не може да го изтрие.** Преди проверката текущият viewport не показваше целия огромен текст, затова не представяме чистата начална снимка като възпроизведен кадър на първоначалното закриване.

Обектът е **скрит**, без изтриване на съдържанието или другите рисунки. Проверени са `Show`/зачеркнатото око в Object Tree и запазеният layout. Стратегията е отделен обект и остава видима. Съществуващият Fib retracement е оставен непроменен.

Доказателства: [съдържание и hash](platform_evidence/text_drawing_diagnosis.json), [Text dialog](platform_evidence/text_drawing_dialog.png), [скрит обект](platform_evidence/text_drawing_hidden.png), [начален viewport](platform_evidence/before_chart.png). Запазеният Pine Editor е отделно прочетен и съвпада с оригиналния baseline: [editor hash](platform_evidence/editor_before.json).

## Как да премахнеш или скриеш такъв текст

1. От дясната лента отвори **Object Tree and Data Window → Object tree**.
2. Избери конкретния **Text** обект. Двойно щракване отваря съдържанието за разпознаване; затвори с **Cancel**, ако само проверяваш.
3. Десен бутон върху реда → **Hide** скрива само него. Обратно се показва с **Show**/иконата око.
4. За премахване използвай **Remove** в менюто на същия ред или кошчето на избрания drawing. Не използвай общото Remove objects, ако искаш да запазиш останалите обекти.
5. Запази layout. При поставяне на код първо щракни вътре в **Pine Editor**, провери, че се вижда редакторският курсор, и тогава постави текста.

Тези действия съответстват на [официалните drawing controls](https://www.tradingview.com/support/solutions/43000484389-how-to-restore-hidden-drawings-indicators-charts/) и [описанието на drawing tools](https://www.tradingview.com/support/solutions/43000703396-drawing-tools-available-on-tradingview/).

## Отделната Pine проверка

Старите chart labels са кратки полета и ценови нива; дългите `f_nativeMessage`/`f_readableEvent` низове са alert текст, а не извикване за изрисуване на изходния код. Старият `f_wrapUi` не разделя единична дълга дума/ID, а общите причини могат да раздуят таблицата. Това са отделни поводи за контролирано ограничаване на видимото обяснение, а не доказателство, че Pine е произвел Text drawing.

Промените и границите на новия rendering слой са в [UI_PINE_IMPLEMENTATION](UI_PINE_IMPLEMENTATION.md). Цените, SL/TP и пълните ID не се режат като обикновен коментар. Резултатите от реална компилация и визуалните режими се отчитат отделно в [TRADINGVIEW_ACCEPTANCE](TRADINGVIEW_ACCEPTANCE.md).
