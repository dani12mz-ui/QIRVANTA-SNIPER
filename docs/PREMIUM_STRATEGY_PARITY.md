# Premium · паритет на стратегията

**`STATIC_SOURCE_GUARD_PASS` / `PREMIUM_NATIVE_PARITY_PENDING`**. Генераторът приема единствено двата пиннати SHA-256 на `QIRVANTA_UNIFIED.pine` и `QIRVANTA_TRADINGVIEW_FINAL.pine`. За Premium разрешава конкретни разлики в заглавие, лимити за рисуване и визуални Inputs; всички други байтове до границата на рендерера, включително порядъка на функциите и order логиката, трябва да съвпадат. Alert tail съвпада байт по байт. Новият рендерер не може да съдържа `strategy.entry/order/exit/close/cancel` или `request.*` заявки. S0 и S5 запазват правилата си; няма нов изследователски филтър в Pine.

Локалният [машинен отчет](premium_evidence/baseline_parity.json) възпроизвежда шест архивни клетки S0/S5 × M5/M15/M30, включително отхвърляне на log файл с SHA извън запазения архив. Отчетът **няма Premium кандидат журнал**. Статичният паритет не удостоверява компилация, графичен runtime или равен нативен резултат на Premium.

При безопасна тестова графика:

1. Компилирай пълните Pine файлове; провери отчетите на графика при еднакви Properties и точно съвпадащ период. За Premium повтори с `PREMIUM` и поне `STANDARD`, без промяна на стратегията. Копирай trade rows и снимай native P&L/PF/drawdown и Properties.
2. Създай отделни lean observer буфери с `python -B unified/tools/make_premium_observer.py ../premium-M5.txt --timeframe 5` и съответно M15/M30. Събери по отделен лог за S0 и S5. Този диагностичен буфер изключва визуалната част; не е release.
3. Подай шест оригинални и шест Premium експорта на `python -B -m unified.tools.audit_premium_parity --baseline-logs … --candidate-logs … --output ../comparison.json`. Сравняват се dataset fingerprint, submission time/bar, план/вход ID, посока, референция, начален SL, цели, реален fill, всяка изходна цена/количество/време/такса, P&L, PF и native max drawdown. Не се допуска замяна на пълен журнал с обобщение.
4. За динамичен SL сверявай допълнително ordered `NATIVE_STOP_SCHEDULED` по план ID и effective/known time при включено логване в пълните версии; частичните exit rows не представляват целия път на стопа.

При несъответствие спри приемането и запази всички разграничени оригинални и кандидат данни. Параметричните хипотези се изпълняват като отделни експерименти след паритета, а не като част от визуалната поправка.
