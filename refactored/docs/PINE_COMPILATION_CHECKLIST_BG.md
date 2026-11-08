# Pine Editor — оставаща реална проверка

Не е изпълнена в тази среда. Няма измислен CLI компилатор или непотвърден PASS.

1. Копирай целия PRO файл в нов Pine Editor tab. Save/Add to chart. Запиши timestamp, build SHA256, compiler errors/warnings и screenshot. Не смесвай остатъци от стария код.
2. Повтори за VALIDATION в отделен tab. Провери, че интерфейсът показва Strategy Tester и simulated trades, а не само indicator table.
3. Проверявай типовете на UDT/array references, object field updates, `input.time`, integer font size и native order args по текущата официална v6 документация. Статичните тестове не проверяват qualifiers.
4. Запази всички runtime warnings на M5/M15/M30/H1/H4 и при недостатъчна история. Провери warmup и графики със стандартни свещи.
5. Реален resource count и performance: дълга история, default18labels и max60labels; 5 текущи линии/labels; не допускай historical buffer errors. Ниският static plot call count не е компилаторен сертификат.
6. При Bar Replay сравни CONFIRMED ids/времена преди/след reload. Старите технически модели не трябва да се връщат към по-ранен pivot.
7. На realtime незавършена свещ окончателни alerts/labels не трябва да се появяват. EMA линиите могат да се променят като текущ анализ.
8. Само след успешна реална компилация промени PINE_COMPILE статуса, като запазиш доказателствата. Не маркирай OOS/forward като PASS заради компилацията.
