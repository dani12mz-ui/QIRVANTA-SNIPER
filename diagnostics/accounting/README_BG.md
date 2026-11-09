# Native журнал и цели входове

Този инструмент чете действителни native редове и метаданни за подадените Plan. Не изпълнява Pine, не измисля липсващи сделки от totals и не променя стратегията. Файловете на този модул са само в `diagnostics/accounting/`; старите източници, тестове и отчети остават baseline.

От корена на repository:

```sh
python diagnostics/accounting/run_tests.py
python diagnostics/accounting/audit.py diagnostics/evidence/B-ALL.pine-logs.csv --origin REAL_TRADINGVIEW --out diagnostics/accounting/B-ALL.audit.json
python diagnostics/accounting/compare_runs.py
```

`audit.py` приема NDJSON или оригиналния **Pine Logs → Download logs** CSV с колони `Date,Message`, включително многоредови log batches. Това е различен достъпен export от платения Strategy Report trade CSV. Не се заобикалят ограничения на плана. Първоначалните headers нямат `origin`; опцията `--origin` го добавя само в прочетените метаданни, записва изрично допълването и не променя суровия файл.

Основните резултати са [RUNS_BG.md](RUNS_BG.md), [run_comparison.json](run_comparison.json) и `B-*.audit.json`. Експерименталният warning observer запазва всичките 286 native records на B-ALL точно: [probe_journal_comparison.json](probe_journal_comparison.json). Това включва 72 submitted Plan, 212 closed rows, run header и snapshot. Не означава доказано realtime/rollback поведение.

## Schema `qirvanta.native.v1`

Един файл съдържа само един `run_id`. Всеки record има `kind` и `run_id`; header има задължителен `schema`. При подаване на `schema` в други records той трябва да съвпада.

| kind | Задължителни полета |
|---|---|
| `run` | `schema`, `symbol`, `timeframe`, `source_sha256`, `origin` (`REAL_TRADINGVIEW` / `SYNTHETIC`), `account_currency`, `symbol_currency`, `pointvalue` |
| `submitted` | `plan_id`, `entry_id`, `submitted_time`, `submitted_bar`, `quantity`, `family`, `side`, `htf_bias`, `regime`, `reference`, `initial_sl` |
| `closed` | `trade_index`, `entry_id`, `entry_time`, `entry_bar_index`, `size`, `entry_price`, `exit_id`, `exit_comment`, `exit_time`, `exit_bar_index`, `exit_price`, `profit`, `commission`, `max_drawdown`, `max_runup` |
| `snapshot` | `at_time`, `at_bar`, `closed_count`, `first_closed_index`, `open_complete`, `open`, `netprofit`, `max_drawdown` |

`size` е signed: положително long, отрицателно short. `quantity` е положителното общо заявено количество. В QIRVANTA `entry_id=plan_id`; family е 1/2/3, side е ±1. `htf_bias` е −1/0/+1 или explicit null, ако не е известен. `open` съдържа всеки оставащ native вход с `entry_id`, `entry_time`, `entry_bar_index`, signed `size` и `entry_price`; празен списък означава flat само с `open_complete=true`.

Допълнителните submitted `countertrend`, `local_aligned`, `efficiency`, `atr`, TP1–3 и maxhold се запазват непроменени. `htf_bias=0` се отчита отделно като NEUTRAL_HTF, дори оригиналният countertrend flag да е true. `mintick` в run е нужен за валиден R знаменател; липса на tick, неположителен или sub-tick начален риск оставя R undefined, но cash не изчезва. `submitted_count` в snapshot проверява пълнотата на Plan метаданните.

Optional run `profit_semantics` е `UNVERIFIED`, `NET_INCLUDES_COMMISSION` или `GROSS_EXCLUDES_COMMISSION`. Всички prices/quantities/cash се смятат с Decimal. Входовете с различни symbol/account валути се отказват: няма скрита FX конверсия. Native slippage вече е в действителните fill prices.

CSV native секцията е ограничена от `QIRVANTA_DATA_BEGIN` / `QIRVANTA_DATA_END`. Отделните `QIRVANTA_BARS_BEGIN/END` rows се броят и хешират, но не създават сделки. Само изрично разпознат `QIRVANTA_WARNING_PROBE` със schema `qirvanta.warnings.v1` и kind `history_scope` се запазва като отделно диагностично доказателство. Неизвестни записи, произволен шум, NaN и непълни секции се отказват.

## Групиране и пълнота

Ключът на една действителна входна кампания е `(run_id, entry_id, entry_time, entry_bar_index)`. Различни entry instances не се сливат по сходна цена или timestamp. Повторен entry_id с повече от една instance се показва изрично като непълен/нееднозначен за тази QIRVANTA схема. Идентично повторен closed index е idempotent; променен record със същия index е грешка.

COMPLETE изисква подаден Plan, съгласувани side/entry prices, сбор на closed quantity равен на submitted quantity и отсъствие от пълния open snapshot. Един market close може да затвори две оставащи единици в един native ред; не се изискват точно три реда. PARTIALLY_OPEN запазва реализирания cash, но не влиза в статистиката на завършените кампании. Липсващи части, metadata, open snapshot или native indices не се дописват. Непопълнена заявка без наблюдаван fill остава отделна категория, без измислена причина за отказ.

Глобалната проверка изисква всеки closed index от 0 до `closed_count−1`. Пропуски и platform trimming маркират INCOMPLETE. Противоположни страни, quantity над заявеното, бъдещи exits спрямо snapshot, конфликтни дубликати и несъвместими валути са входни грешки. CLI не позволява output да презапише raw input.

## Cash, PF и drawdown

Gross движение = `(exit_price−entry_price) × signed_size × pointvalue`; net = gross движение − **native entry+exit commission веднъж**. Сборът се сравнява с `snapshot.netprofit`, само когато всички closed rows са налични. Raw API `profit` се сравнява отделно с gross и net; не се предполага автоматично дали fee е включена. При действителните B-ALL 212/212 records съвпадат с **вече net** и сборът е точно −144.492 USD. Повторно изваждане на commission от този API profit би било грешно.

Pine reference описва `commission()` като общите entry+exit такси; текстовият v6 reference е динамичен и сам по себе си не беше достатъчен за недвусмислен извод за `profit()`. Затова семантиката е проверена емпирично спрямо всеки native fill и native netprofit. [Reference](https://www.tradingview.com/pine-script-reference/v6/#fun_strategy.closedtrades.commission), [официални gross profit правила](https://www.tradingview.com/support/solutions/43000681686-gross-profit/), [gross loss](https://www.tradingview.com/support/solutions/43000681688-gross-loss/).

PF на **цели кампании** използва положителния/отрицателния общ net на всеки завършен вход; PF на **native rows** използва отделните partial outcomes. Броят, win rate и средната печалба/загуба се отчитат с ясна единица. При липса на загуби PF е null с `NO_LOSSES`, а не измислен finite PF. Срезовете по family/side/HTF/regime са атрибуция на действителния портфейл, а не отделни симулации.

Всеки от тези срезове съдържа и отделни realized closed-cash/campaign-close drawdown, raw exit-comment counts, затворени количества по exit-comment и последен exit-comment по цяла кампания. Причина SL след TP1 може да приключи печеливша кампания; не се преименува автоматично на загуба. Drawdown на такова подмножество не се представя като самостоятелен backtest на модел.

`closed_cash_drawdown` е peak-to-trough на реализираните export exits, в journal order при равни timestamps. `complete_campaign_close_drawdown` признава цялото net чак на последния exit на кампанията. И двете са отделни от предоставения native equity/intrabar max drawdown; per-part MAE/MFE никога не се сумират или максимизират като предполагаемо MAE/MFE на целия вход. [Официален intrabar max drawdown](https://www.tradingview.com/support/solutions/43000681690-max-drawdown-intrabar/).

Статус PASS_OBSERVED_JOURNAL доказва съгласуване на изнесения журнал в посочения обхват. Не доказва пазарно предимство, бъдеща доходност, причината за всеки исторически отказ, нито резултат на ненаправен strategy run.
