# QIRVANTA Ultimate · инженерен отчет

## A. Резюме

Отделната Pine v6 стратегия премина реална M15 компилация и изпълнение. Новият Python пакет съчетава причинно изследване, дванадесет аналитични роли, независим Risk Guardian, локален paper журнал, календарен интерфейс и ограничен MT5 reader. Базовият Pine файл, историческите резултати и предишните регистрации са фиксирани от началното Git дърво. [Базов manifest](ultimate_evidence/baseline.json)

## B. Статус по фази

| Фаза | Доказан обхват | Непокрито |
|---|---|---|
| [1 · Pine](PHASE1_ENGINEERING.md) | `PLATFORM_VERIFIED` за чиста компилация, M15 A/B и четири режима. `STATICALLY_VERIFIED` за R:R и обекти | Активен mobile план, физически iPhone и нова technical alert доставка |
| [2 · Стратегии](PHASE2_PROFITABILITY.md) | `LOCALLY_TESTED` за 77 исторически сценария. `PLATFORM_VERIFIED` за F1 | `FAILED_VALIDATION` за подобрение при F1 в съпоставимия native тест. Независим период `BLOCKED_EXTERNAL` |
| [3 · ML](PHASE3_AI_INTELLIGENCE.md) | `LOCALLY_TESTED` за локално обучение, калибрация и inference | Независима оценка и production inference `BLOCKED_EXTERNAL` |
| [4 · Структура](PHASE4_MARKET_STRUCTURE.md) | `LOCALLY_TESTED` за причинни OHLC характеристики и proxy стратегии | Дълбоки обемни и tick/trade наблюдения `BLOCKED_EXTERNAL` |
| [5 · Риск](PHASE5_RISK_MANAGEMENT.md) | `LOCALLY_TESTED` за sizing, вето, управление, резервации и стрес | Реални PU Prime спецификации и финансиране `BLOCKED_EXTERNAL` |
| [6 · Макро](PHASE6_MACRO_NEWS.md) | `LOCALLY_TESTED` за календарни snapshots, наличност във времето и fail-closed | Непрекъснат проверен календар, consensus и news NLP данни `BLOCKED_EXTERNAL` |
| [7 · Анализатори](PHASE7_MULTI_AGENT.md) | `LOCALLY_TESTED` за 12 отделни роли, конфликти, timeout и риск | Външно моделно обслужване `NOT_IMPLEMENTED` |
| [8 · MT5](PHASE8_MT5_INTEGRATION.md) | `LOCALLY_TESTED` за reader, metadata, локални намерения и сравнение на серии | Реални broker account и символи `BLOCKED_EXTERNAL` |
| [9 · Forward](PHASE9_FORWARD_VALIDATION.md) | `LOCALLY_TESTED` за prospective регистрация, симулиран lifecycle и възстановяване | Независими бъдещи наблюдения `BLOCKED_EXTERNAL` |
| [10 · Операции](PHASE10_PRODUCTION.md) | `LOCALLY_TESTED` за foreground CLI, диагностика, SQLite и Bulgarian journal notifications | Активна външна услуга, Telegram доставка и operational acceptance `BLOCKED_EXTERNAL` |

## C. Компоненти

Pine генераторът прилага малки и проверими промени върху точния champion. Research пакетът съдържа F0–F12, exit експерименти, S1–S10 и A–L правила с отделни регистрации. Оперативният пакет пази конфигурации, проследими решения, journal записи и симулирани fills. MT5 reader и journal за локални намерения са отделени от paper обработката. [Pine](../unified/experimental/QIRVANTA_ULTIMATE_EXPERIMENTAL.pine) · [Research](../research/ultimate/run.py) · [Service](../qirvanta_ultimate/__main__.py)

## D. Проверки

Минаха 273 локални теста. Изолираният процес стартира CLI от чист изходен архив и hash-проверения `tzdata` wheel. Това доказателство е отделно от TradingView compiler/runtime, проверката на MT5 предпоставки, действителното ML обучение и симулирания paper lifecycle. Историческият UI журнал се възпроизведе байтово от локалния raw export. В A/B всички барове и HTF полета съвпаднаха, както и всички OFF native кампании спрямо архива след изключване на `run_id`. [Интеграционен запис](ultimate_evidence/integration.json) · [Платформено доказателство](PHASE1_ENGINEERING.md) · [Независим преглед](ultimate_evidence/review.json)

## E. Съпоставима доходност

OANDA:XAUUSD M15, 02.08.2026 22:00–09.11.2026 02:45 UTC. Размер 3, комисиона 0,175 USD на единица за изпълнение, 2 ticks slippage, решение на затворена свещ и fill на следващ тик. Данните и разходите са еднакви в двата native runs.

| Показател | Champion / F1 OFF | F1 ON |
|---|---:|---:|
| Кампании / части | 72 / 212 | 69 / 203 |
| Win rate | 45,83% | 44,93% |
| Net USD | −144,492 | −261,758 |
| Profit factor | 0,905096 | 0,818867 |
| Средно net R | −0,10206 | −0,14264 |
| Средна печалба / загуба, USD | +41,758 / −39,039 | +38,173 / −38,029 |
| Native equity DD, USD | 842,255 | 770,437 |
| Най-дълъг реализиран cash drawdown, дни | 55,58 | 55,58 |
| Описателен 95% интервал на mean R | [−0,3603, +0,1736] | [−0,3851, +0,1071] |
| Кампании с TP1 / TP2 / TP3 | 32 / 16 / 10 | 30 / 14 / 9 |

Cash drawdown продължителностите са цензурирани на cutoff. F1 намалява наблюдавания DD и влошава net с 117,266 USD. 77 Python сценария остават exploratory, с нула Holm-adjusted p под 0,05. Всички кампании и частични изходи са запазени. [Native A/B](ultimate_evidence/native_f1_comparison.json) · [Пълен сравнителен отчет](PERFORMANCE_COMPARISON.md)

## F. AI и статистика

Действително са обучени локални logistic модели, Platt калибратори при достатъчни класове и ridge модел за net R. Целите включват положителна кампания и TP1/TP2/TP3 преди SL. При 15 по-късни, вече наблюдавани кампании net-positive Brier е 0,25246 за константа, 0,24732 за logistic и 0,23865 за Platt. Калибрираната селекция е с нула приети кампании при фиксирания праг. Резултатите не дават основание за активиране. [Подробности за моделите](PHASE3_AI_INTELLIGENCE.md)

## G. Дванадесет роли

Режим, структура, ликвидност, momentum, волатилност, MTF, макро, стратегия, вход, Risk Guardian, управление и независим одит имат отделни входове, проверки и изходи. Решението пази причините за отказ, конфликт и липсващ вход. Изпълнените роли са детерминистични. ML изследването е отделно. [Матрица на ролите](PHASE7_MULTI_AGENT.md)

## H. MT5

Действителният probe откри нула работещи терминални пътя и липсващ `MetaTrader5` SDK в наличния Python. Потвърдени PU Prime account, `XAUUSD.s`, BTC символ, котировки и спецификации липсват. Инспекцията изисква вече работещ и свързан терминал с точно зададени идентичности. Demo и live execution режимите са блокирани. [MT5 статус](PHASE8_MT5_INTEGRATION.md)

## I. Риск и сигурност

Рискът проверява quote age, spread, геометрия, позиционен и портфейлен бюджет, дневен и седмичен остатък, маржин, сесия и паузи. Журналът има транзакции и hash chain. Локалният paper профил задава явни разходи и собствен размер на договор. Пакетната проверка намери нула отчетени advisories за точно `tzdata` 2026.6 в запитаните регистри. [Риск](PHASE5_RISK_MANAGEMENT.md) · [Сигурност](SECURITY_AND_SAFETY.md) · [Проверка на зависимост](ultimate_evidence/dependency_audit.json)

## J. Forward

Операторът може да регистрира бъдещ прозорец и да стартира foreground monitor с разрешен поток. Регистрацията пази имплементация, профил и параметри. Измерените paper тестове са локални lifecycle проверки. За prospective доказателство са нужни наблюдения, започнали след регистрация и достатъчни за заявения протокол. [Forward протокол](PHASE9_FORWARD_VALIDATION.md)

## K. Готовност

Research режимът е възпроизводим от заключените входове. Локалният paper режим е функционално проверен. Проверен broker/demo, активна background услуга, независима paper оценка и live готовност липсват. [Ръководство за стартиране](DEPLOYMENT_GUIDE_BG.md)

| Gate | Статус |
|---|---|
| A · Code integrity | `STATICALLY_VERIFIED` |
| B · Deterministic behavior | `LOCALLY_TESTED` |
| C · Data integrity / causality | `LOCALLY_TESTED` в документирания обхват |
| D · Native platform | `PLATFORM_VERIFIED` за Pine M15. Реална MT5 сесия `BLOCKED_EXTERNAL` |
| E · Разходи и изпълнение | Съпоставими native настройки и симулационни стресове. Реални PU Prime разходи `BLOCKED_EXTERNAL` |
| F · Независима статистика | `BLOCKED_EXTERNAL` |
| G · Prospective paper | `BLOCKED_EXTERNAL` |
| H · Смяна на режим на изпълнение | Блокирана в доставения код |

## L. Репозиториум

Клон `engineering/qirvanta-ultimate-phases-1-10`, наследник на `c2212d7b7e0b4b7ea4be64c1fb3c9f7b33fa0f71`. Сравнението в GitHub показва точния публикуван head. Хешовете на кода и доказателствата са в manifest. [Сравнение](https://github.com/dani12mz-ui/QIRVANTA-SNIPER/compare/c2212d7b7e0b4b7ea4be64c1fb3c9f7b33fa0f71...engineering/qirvanta-ultimate-phases-1-10) · [Възпроизвеждане](REPRODUCIBILITY.md) · [Manifest](ultimate_evidence/release_manifest.json)

## M. Външни блокери

Работещ PU Prime MT5 и SDK, проверен свеж quote поток и договорни условия, надежден лицензен календар с наличност във времето, независим бъдещ период, подходящо разрешение за technical alerts и реална проверка на iPhone/Telegram доставка. [Проверен обхват](KNOWN_LIMITATIONS.md)

## N. Решение

**PARTIALLY_COMPLETE.** Доставени са работещи research и локални paper компоненти в проверения обхват. F1 получава `REJECT`. За останалите перспективни варианти решението е `INSUFFICIENT_EVIDENCE`. Champion остава върху фиксирания production файл. [Правила за избор](STRATEGY_CHAMPION_CHALLENGER.md)
