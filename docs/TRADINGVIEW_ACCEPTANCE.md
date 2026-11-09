# Реална проверка в TradingView — 09.11.2026

**Pine v6 компилацията и изпълнението са проверени. Търговско предимство, активен SL/TP план на телефон и strategy push не са доказани.** Единственият production файл е `unified/QIRVANTA_UNIFIED.pine`, SHA256 `92307768be68946dc87cbe4ea45a8b5e32446fd3b7a0c1a38dce40dffe358be3`. Записан е в личния профил като QIRVANTA_UNIFIED, версия 17. Временните наблюдатели са премахнати; clipboard readback съвпада точно с файла.

## Компилация и настройки

Последното възстановяване на чистия код е компилирано/записано в 19:59:24 Europe/Sofia (17:59:24 UTC). Доказателства: [компилация](platform_evidence/restored_pure_compile.txt), [прочетен обратно код](platform_evidence/restored_editor_readback.json). Няма нова compile error. Остават четири предупреждения: `f_fast`, `f_reversal` и две за `barstate.islast` при close-only стратегия. Те не са премахвани механично; анализът и ограниченията са в [BASELINE_AUDIT](BASELINE_AUDIT.md).

При първия UI build имаше допълнителни CW10026 за групирани Input заглавия. Редовете са разделени и тези четири предупреждения вече отсъстват. Старите compile съобщения в общата editor console не са грешки на окончателния артефакт; доказателствата различават времето и версията.

[Baseline настройки](platform_evidence/baseline_settings_verified.json): 108/108 Inputs. [Окончателни настройки](platform_evidence/final_settings_verified.json): 118/118 Inputs, всички съвпадат с текущите defaults; 11 Properties групи съвпадат с възпроизведения baseline. Символ OANDA:XAUUSD, стандартни Candles, quantity 3, 100 000 USD начален симулиран капитал, commission 0.175/unit/fill, slippage 2 ticks, pyramiding 0, default bar детализация, on-bar-close, next-tick execution. Изследователският cutoff не променя входните дати или стратегията: наблюдателят записва наличното състояние към фиксирания час.

## Възпроизвеждане на M15

Повторно изпълнени са непромененият baseline и UI версията върху **едни и същи 6 445 бара**, 02.08.2026 22:00–09.11.2026 02:45 UTC. SHA256 на повторно наблюдавания CSV: `6c89015105a406c2a0c1567f0e9a5e445cb31ee7604a985d727f26892d8ba4b7`.

Съвпадат всички 72 цели позиции, 212 native части, действителни fills/количества/такси/изходи, net **−144.492 USD**, PF **0.905096**, native equity DD **842.255 USD**. Единственото изключено поле при сравнението е run label; няма изключени икономически полета. [Пълно доказателство](platform_evidence/ui_native_parity.json).

Спрямо стария архив доставчикът е ревизирал 33 volume стойности; OHLC/HTF и всички кампании са идентични. Старият архив остава непроменен. По-късният видим Tester включва нови барове и различни резултати; той не се подменя с baseline. [Възпроизвеждане](platform_evidence/baseline_reproduction.json).

## Runtime и действителни данни

| Рамка | Проверено в чист production | Доказателство |
|---|---|---|
| M5 | Графика и табло след преизчисляване; без runtime error | [снимка](platform_evidence/M5_STANDARD_desktop.png), [AX](platform_evidence/M5_runtime.txt) |
| M15 | Четирите режима, native parity, текущо табло | [снимка](platform_evidence/delivery_M15.png), [AX](platform_evidence/delivery_state.txt) |
| M30 | Графика, табло, H1/H4 контекст; без runtime error | [снимка](platform_evidence/M30_STANDARD_desktop.png), [AX](platform_evidence/M30_runtime.txt) |
| H1 | Равна H1 рамка използва затворена локална свещ; H4 контекст | [снимка](platform_evidence/H1_STANDARD_desktop.png), [AX](platform_evidence/H1_runtime.txt) |
| H4 | H4 локален контекст; H1 изрично недостъпен | [снимка](platform_evidence/H4_STANDARD_desktop.png), [AX](platform_evidence/H4_runtime.txt) |

Извлечени са и действителни M5/M30 журнали с read-only приложения към точния production prefix. Наблюдателят няма нови order calls и отказва грешна рамка. M30 cutoff е 02:30, защото 02:45 не е негов close. Пълните нови OHLC/log exports са локални; публикувани са hashes, manifests и derived accounting.

| Native извадка | Бара / период UTC | Цели позиции / части | Net USD | PF цели позиции | Native equity DD |
|---|---|---:|---:|---:|---:|
| M5 | 5 565; 11.10 22:00–09.11 02:45 | 46 / 134 | +50.438 | 1.101025 | 217.513 |
| M15 | 6 445; 02.08 22:00–09.11 02:45 | 72 / 212 | −144.492 | 0.905096 | 842.255 |
| M30 | 10 091; 01.01 23:00–09.11 02:30 | 97 / 283 | −376.557 | 0.903627 | 1595.163 |

Тези три реда имат **различни периоди** и не доказват кой timeframe е по-добър. Отделният регистриран common-window анализ е в [EXPERIMENT_RESULTS](EXPERIMENT_RESULTS.md). M5/M30 accounting проверките нямат липсващи индекси, отворени позиции или касова разлика в съответните фиксирани snapshots: [M5](platform_evidence/M5_native.audit.json), [M30](platform_evidence/M30_native.audit.json).

## Визуална проверка и граници

MINIMAL, STANDARD, PRO и DEBUG са реално показани. STANDARD е възстановен; history, zones и debug archive са OFF. Числата не се съкращават. Външният Text drawing с 115 724 знака е скрит през Object Tree; не е Pine label. [Диагноза](UI_DIAGNOSIS.md).

Снимките [MINIMAL](platform_evidence/M15_MINIMAL_desktop.png), [STANDARD](platform_evidence/M15_STANDARD_desktop.png), [PRO](platform_evidence/M15_PRO_desktop.png), [DEBUG](platform_evidence/M15_DEBUG_desktop.png) не показват гигантски Pine текст. При умишлено включени DEBUG+history+zones има припокриване на съседни исторически надписи. Native order markers също могат да попадат върху таблото на H4. Не се твърди универсална липса на overlap; историята по подразбиране е OFF и позицията на таблото е настройваема.

[Мобилен viewport 390×844](platform_evidence/M15_STANDARD_mobile_390.png) показва четливо компактно табло без активна позиция. Няма физически iPhone тест. Нито една от петте рамки не показа активен план в текущия наблюдаван момент. Опитът за replay на действителен исторически план на 04.11 в 13:00 е блокиран от Basic intraday Replay: [доказателство](platform_evidence/replay_blocked.txt). Следователно активен Entry/SL/TP1–TP3 mobile acceptance е **NOT VERIFIED**, а не PASS от синтетична картинка. Replay и временният viewport са изключени след проверката.

## Известия

Create Alert вижда QIRV ONE v17, M15 и точния template с `strategy.order.alert_message`, действителни emulator price/qty/order ID и UTC часове. Изборът е order fills + alert() calls. Реалният опит за запис е отказан: **Basic — лимит 0 technical alerts**, [текст](platform_evidence/technical_alert_blocked.txt), [снимка](platform_evidence/technical_alert_blocked.png). Няма активен сървърен alert от стратегията.

Платформата показва и предупреждение за възможно repainting: [наблюдаван текст](platform_evidence/alert_warning.txt). Това не е потвърден конкретен future-leak дефект и не доказва обратното. Причинната проверка на source, затворените HTF семантики, наблюдаваната parity и provider revisions са отделни доказателства с ограничен обхват. [Официалната документация](https://www.tradingview.com/pine-script-docs/concepts/repainting/) различава незатворени стойности, бъдещи данни и ревизии на доставчика; не даваме универсална гаранция за всички settings/feed states.

Разрешеният отделен **ценови информационен тест** XAUUSD > 1, Once only, е доставен в TradingView toast в **18:02:51 UTC**. Текстът изрично казва „ТЕСТ НА ДОСТАВКА… НЕ е търговски сигнал или брокерска поръчка“. [Конфигурация](platform_evidence/info_alert_test_configuration.txt), [доставка](platform_evidence/info_alert_delivered.txt), [снимка](platform_evidence/info_alert_delivered.png), [Stopped — Triggered](platform_evidence/info_alert_stopped.txt). Това проверява разрешения транспорт до уеб профила, не strategy fill доставка или физически телефон. Няма webhook/MT5 поръчки.

Запазването на Pine не създава сървърен alert; alert използва snapshot и трябва да бъде пресъздаден след промяна. Историческите сделки не изпращат нови realtime известия. [TradingView Alerts](https://www.tradingview.com/pine-script-docs/concepts/alerts/). Подробните български полета са в [MOBILE_ALERTS](MOBILE_ALERTS.md).
