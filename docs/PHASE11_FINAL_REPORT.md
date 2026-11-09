# QIRVANTA · Фаза 11

## A. Компоненти

`IMPLEMENTED` и `LOCALLY_TESTED` за read-only MT5 адаптер, bounded poller, седем UTC агрегации, отделен native history cache, 12 аналитични роли, независими paper портфейли, Risk Guardian, SQLite и българска опашка за известия. [Код на foreground услугата](../qirvanta_ultimate/phase11.py) · [Поток от данни](PHASE11_DATA_PIPELINE.md)

## B. Проверени тестове

Минаха 313 теста, от които 113 за Pine и съпътстващия пакет, 119 за research и 81 за локалната услуга. Регресиите обхващат duplicate/stale/future котировки, session gaps, повторно свързване, замразен регистър, договорни ограничения, SL/TP, рестарт, губеща позиция, частични изходи, автентикация и доставка. Архивният run е върху 6 445 OANDA:XAUUSD M15 свещи, 02.08.2026 22:00–09.11.2026 02:45 UTC, с проверен SHA-256. Срезът изпълнява 12 роли при последното затваряне. В него липсват свеж брокерски bid/ask и одобрен календар и няма paper кампании. [Възпроизводими команди и резултати](phase11_evidence/validation.json) · [Тестов лог](phase11_evidence/tests.txt)

## C. Реален MT5 статус

`BLOCKED_EXTERNAL`. На Windows е установен CPython 3.12.14 x64. Проверката откри 0 работещи MT5 терминала и липсващ MetaTrader5 Python SDK. Не е извършено прикачване. Няма прочетени акаунт или позиции, променени настройки или разходи към брокер. [Проверка на средата](PHASE11_ENVIRONMENT_DISCOVERY.md) · [Read-only адаптер](PHASE11_MT5_CONNECTIVITY.md)

## D. XAUUSD.s и BTCUSD

`BLOCKED_EXTERNAL` за откриване и на двата инструмента. XAUUSD.s е поисканото име, а точният BTC символ, договорната спецификация, валутата на печалбата, котировките и часовете зависят от наличния брокерски профил. В примерния JSON числата са демонстрационни. [Примерна конфигурация](phase11_evidence/runtime.example.json)

## E. Данни в реално време

`LOCALLY_TESTED` за наблюдавани bid/ask/last, възраст, spread, монотонен sequence, UTC M1/M5/M15/M30/H1/H4/D1, отсъстващ интервал, рестарт и неизменяеми native snapshots. `BLOCKED_EXTERNAL` за действителни PU Prime ticks, нативни свещи и broker uptime. По-рано извлечените native bars могат да подготвят контекста, ако са хеширани преди регистрацията. Допуск се оценява само след нова потвърдена свещ от потока. [Политика за данните](PHASE11_DATA_PIPELINE.md)

## F. Дванадесет роли

`LOCALLY_TESTED` за regime, structure, liquidity, momentum, volatility, MTF, macro, strategy, entry, management, risk и audit. Това са детерминистични специализирани функции. Журналът свързва последния резултат с източник, наличност на данните, време и trace. Без текущо календарно покритие услугата дава `NEWS_DATA_UNAVAILABLE`. [Код](../qirvanta_ultimate/intelligence.py) · [Архивна проверка](phase11_evidence/validation.json)

## G. Paper симулация

`LOCALLY_TESTED`. В синтетичен S5 тест 3 частични TP затварят една кампания с нетен резултат +84,07 USD. Независим тест със същия вход и прекъсване завършва при стоп с −55,50 USD и флаг за несигурен ценови път. Изходите са записани на първата действително подадена към теста годна котировка. Няма пренесен синтетичен резултат в реален отчет. [Paper модел](PHASE11_PAPER_ENGINE.md) · [Числа и допускания](phase11_evidence/validation.json)

## H. Risk Guardian

`LOCALLY_TESTED` за свежест, spread, валута и точна спецификация, сесия, SL/TP геометрия, минимални отстояния, размер по обемна стъпка, дневен/седмичен лимит, концентрация на независимия портфейл и новини. Повторна оценка е нужна и при по-късната котировка за вход. Липсващият източник блокира нова експозиция. [Проверки на риска](PHASE11_RISK_VALIDATION.md)

## I. Champion и challengers

Фиксирани са четири отделни портфейла за всеки инструмент, CHAMPION, S3, S5 и S8. Локалният интерфейс за champion валидира Pine събитие, идентичност и закъснение, след което го сверява с последния затворен брокерски бар и bid/ask при получаване. При липса на съгласувано събитие champion няма нов план. F1 остава отхвърлен и изключен. Оригиналният OANDA M15 резултат остава −144,492 USD, PF 0,905096, 72 кампании и 212 части. Историческият F1 остава −261,758 USD за 69 кампании. [Champion и challengers](PHASE11_CHAMPION_CHALLENGER.md) · [Pine сверяване](PHASE11_SIGNAL_PARITY.md) · [Базов отчет](ULTIMATE_MASTER_REPORT.md)

## J. Telegram

`LOCALLY_TESTED` за българска SQLite опашка, ограничено повторение, обобщения и синтетичен transport contract. `BLOCKED_EXTERNAL` за реална доставка. Липсват вече разрешени бот и чат в достъпната конфигурация. Локалният transport тест не доказва доставка до устройство. [Известия](PHASE11_TELEGRAM.md)

## K. Windows

`LOCALLY_TESTED` за CLI на преден план. Изолираният Windows smoke изпълни `probe`, `register`, `serve`, `status`, `report` и `replay`. Тествахме сигналния прием и broker history модулите с контролирани входове. Командите `signal` и `cache-bars` остават без проверен външен източник. `pause` е операторската команда при спрян foreground. [Инструкции за Windows](PHASE11_WINDOWS_DEPLOYMENT_BG.md)

## L. Сигурност

`LOCALLY_TESTED` за отказ на режими за изпълнение, съвпадение на брокерска самоличност, удостоверяване на gateway, запазване на идемпотентен журнал и разрешен получател. Съществуват read-only извиквания за брокера и симулация в SQLite. Брокерски поръчки, промени и отмени за Фаза 11 са **0**. [Доказателства](PHASE11_SECURITY.md)

## M. Проспективна валидация

`FORWARD_EVIDENCE_INSUFFICIENT`. Има проверен механизъм за предварителна регистрация на бъдещ прозорец и правила за минимум 100 приключени кампании, средно нетно R, PF, drawdown и доверителен интервал. Реалният брокерски прозорец остава нестартиран. Периодът с реални проспективни наблюдения е празен, броят на приключените проспективни кампании е 0, а резултатът е неопределен. [Протокол](PHASE11_FORWARD_PROTOCOL.md)

## N. Външни пречки

Липсват текущ MT5 SDK и терминал, точен broker profile със сесии, одобрен свеж news feed, TradingView gateway и разрешена Telegram конфигурация. При текущата проверка на TradingView потребителската сесия беше прекъсната. Предишният запис на техническо известие е отказан при лимит 0 на Basic. [Граници](PHASE11_LIMITATIONS.md) · [Предишна платформена проверка](TRADINGVIEW_ACCEPTANCE.md)

## O. GitHub

Клон `engineering/qirvanta-phase11-live-paper`, върху родител `f9bcf7430ec0c50d013da93c155fb79f5684bade`. Точният публикуван commit се вижда от историята на този файл и от проверената връзка за доставка. [Клон](https://github.com/dani12mz-ui/QIRVANTA-SNIPER/tree/engineering/qirvanta-phase11-live-paper) · [Сравнение с Ultimate базата](https://github.com/dani12mz-ui/QIRVANTA-SNIPER/compare/f9bcf7430ec0c50d013da93c155fb79f5684bade...engineering/qirvanta-phase11-live-paper) · [Манифест](phase11_evidence/release_manifest.json)

## P. Готовност

**PAPER_INFRASTRUCTURE_READY** за локално възпроизводимия код и **FORWARD_EVIDENCE_INSUFFICIENT** за реално наблюдение. Приетите тестове установяват техническо поведение. Доходност или подобрение на сигналите с реални брокерски данни не са установени.
