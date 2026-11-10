# Premium · Pine v6 компилация

Този отчет описва първоначалния Premium commit `d13b46f`. В [последващата QA проверка](PREMIUM_NATIVE_QA_CONTINUATION.md) са записани точният SHA-256 и размерът на актуалната визуална корекция; пълната нативна компилация остава неизпълнена.

**`FULL_PREMIUM_NATIVE_COMPILATION_NOT_RUN`**. `unified/experimental/QIRVANTA_SNIPER_PREMIUM.pine` е самостоятелен Pine v6 файл с едно `strategy()` и без imports. Визуалните фрагменти и Python инструментите са само за разработка и проверка; в TradingView се поставя единствено пълният `.pine`.

Базовият Pine SHA-256 `bc7bf16cd988fa23832f75ff22ee421b198cafb95614c9445622ce7bc4cfe1c3` е [компилиран в TradingView](tradingview_only_evidence/platform_compile.txt). Документираният резултат **101 088** срещу лимит **100 256** е за *предишния пълен рендерер плюс диагностичен журнал*, а не размер на голия базов Pine или на Premium. Premium е **169 597 байта, 2 320 реда**, SHA-256 `da9d0a43e4eaa7c06ff1b298795c1b082341790ffe9cc665121f824eafa69dec` ([машинен отчет](premium_evidence/baseline_parity.json)). [TradingView](https://www.tradingview.com/pine-script-docs/writing/limitations/) брои токени след компилация, а успешно компилиран скрипт не показва броя им. Локално сравнение на размера на текста не доказва токенен резерв или приемане от платформата.

За ограничаване на размера Premium заменя стария рендерер с общо табло, компактни помощни функции и масиви с горна граница. Няма допълнителни `request.security` извиквания, няма допълнителни `strategy` операции и не са премахвани проверки на ордерите. Нивата се обновяват с setters; етикетите за цени се преизползват. Визуалните данни идват от съществуващия `Event` поток.

```text
python -B -m unified.tools.build_sniper_premium --check
python -B -m unittest unified.tests.test_sniper_premium -v
```

Минаха 131 локални `unified` теста (включително пет нови). За наследените imports `tools.*` при стартиране на целия suite от корена в `PYTHONPATH` се включват коренът и `unified/`. Повторното извличане на архивните сравнения е отделно от Pine компилация.

За нативен приемателен тест постави **целия** `.pine` в Pine Editor на отделна безопасна графика. Запиши резултата от `Add to chart`, всички компилаторни и runtime съобщения, точния SHA-256 на буфера, символа, рамката, настройките и валидни снимки. Ако тестът не минава, запиши пълния текст и не представяй lean observer като заместител на компилацията. [TradingView: създаване на временен draft](https://www.tradingview.com/support/solutions/43000711497-how-to-create-a-new-script/).
